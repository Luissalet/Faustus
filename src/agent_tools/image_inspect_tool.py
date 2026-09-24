"""agent_tools/image_inspect_tool.py — the `inspect_image` tool.

One tool, `{action, ...}` (same shape `pdf_ops`/`git_tools` use for a
multi-action surface), so a single schema covers a targeted look at one image
region instead of trusting the generic "Describe this image in detail"
caption a configured Vision model produces for a text-only main model. See
`src.image_inspection` for the pure crop/rotate/zoom/enhance/grid/shapes
logic this wrapper drives, and `src.document_processor.
analyze_image_with_vl_prompt` for the model call itself.

Actions: ``ask`` (default, a specific question about the processed image),
``view`` (just return the crop), ``shapes`` (local, model-free circle/
rectangle/line detection), ``compare`` (two images, one question),
``grid_locate`` (overlay a lettered/numbered grid and ask which cells match).

Every action that can reach a model chooses one of two paths: when the
model running THIS turn can already see images, the processed image is
attached to the tool result directly (the main model examines it on its own
next turn, same as `read_file` on a picture); otherwise the configured
Vision model is asked the caller's own question via
`analyze_image_with_vl_prompt` — never the generic caption. Every result
also carries the local "measurements" (region used, rotation, grid, whether
the image was downscaled) regardless of which path answered.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src import image_inspection as ii

logger = logging.getLogger(__name__)

_LITERAL_INSTRUCTIONS = (
    "You are examining a SPECIFIC image closely, not writing a general caption. "
    "Be literal about exactly what you see; say \"unclear\" rather than guessing "
    "when something is ambiguous or you cannot tell. When you describe where "
    "something is, give its position as a fraction of the shown image (e.g. "
    "\"about 30% from the left, 50% from the top\"), or by the labelled grid "
    "cell it falls in if a grid is overlaid."
)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_image(args: Dict[str, Any], *, suffix: str = "") -> "ii.LoadedImage":
    path = args.get(f"path{suffix}")
    url = args.get(f"url{suffix}")
    if path and url:
        raise ii.InspectImageError(f"inspect_image: provide either `path{suffix}` or `url{suffix}`, not both")
    if url:
        return ii.load_from_url(str(url))
    if path:
        page = args.get(f"page{suffix}")
        dpi = args.get(f"dpi{suffix}") or ii.DEFAULT_PDF_DPI
        return ii.load_from_path(str(path), page=int(page) if page else None, dpi=int(dpi))
    raise ii.InspectImageError(f"inspect_image: `path{suffix}` or `url{suffix}` is required")


def _process_args(args: Dict[str, Any], *, suffix: str = "") -> Dict[str, Any]:
    return {
        "region": args.get(f"region{suffix}"),
        "units": args.get(f"units{suffix}") or args.get("units") or "fraction",
        "rotate": float(args.get(f"rotate{suffix}") or 0.0),
        "zoom": args.get(f"zoom{suffix}"),
        "max_side": args.get(f"max_side{suffix}"),
        "enhance": args.get(f"enhance{suffix}"),
    }


def _measurements(loaded: "ii.LoadedImage", proc: "ii.ProcessResult") -> Dict[str, Any]:
    out = {
        "source": loaded.source,
        "engine": loaded.engine,
        "region_used": list(proc.region_used),
        "rotate": proc.rotate,
        "zoom": proc.zoom,
        "enhanced": proc.enhanced,
        "grid": list(proc.grid) if proc.grid else None,
        "original_size": list(proc.original_size),
        "output_size": list(proc.output_size),
        "downscaled": proc.downscaled,
        "mapping_note": proc.mapping_note,
    }
    if loaded.page:
        out["page"] = loaded.page
    return out


def _downscale_warning(proc: "ii.ProcessResult") -> str:
    if not proc.downscaled:
        return ""
    return (f" [note: the image was downscaled to {proc.output_size[0]}x{proc.output_size[1]}px "
            "to stay within the size limit]")


def _main_model_can_see(ctx: Dict[str, Any]) -> bool:
    """Reuses `model_supports_vision`/`is_vision_model` (src.chat_helpers) —
    the same check the loop uses to decide whether a tool-result image is
    attached or described. `ctx` carries `turn_model` but no live endpoint
    URL, so this is the name-heuristic half of that check."""
    model = str((ctx or {}).get("turn_model") or "").strip()
    if not model:
        return False
    try:
        from src.chat_helpers import is_vision_model
        return bool(is_vision_model(model))
    except Exception:  # noqa: BLE001
        return False


async def _ask_vision_model(images: List[Tuple[bytes, str]], prompt: str, owner: Optional[str],
                             model_override: Optional[str]) -> Dict[str, str]:
    from src.document_processor import analyze_image_with_vl_prompt
    return await asyncio.to_thread(analyze_image_with_vl_prompt, images, prompt, owner, model_override)


def _images_payload(*imgs_and_mimes: Tuple[str, str]) -> List[Dict[str, str]]:
    return [{"data": b64, "mimeType": mime} for b64, mime in imgs_and_mimes]


def _b64_to_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

async def _action_ask(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    owner = ctx.get("owner") if isinstance(ctx, dict) else None
    question = str(args.get("question") or "").strip() or (
        "Describe exactly what is visible in this image: any text, marks, symbols or "
        "annotations, and where each one is."
    )
    loaded = _load_image(args)
    pargs = _process_args(args)
    proc = ii.process(loaded.image, grid=args.get("grid"), **pargs)
    measurements = _measurements(loaded, proc)
    warn = _downscale_warning(proc)

    if _main_model_can_see(ctx):
        b64, mime = ii.image_to_b64(proc.image)
        text = (f"inspect_image: processed view of {loaded.source} attached "
                f"(region {measurements['region_used']} of the original) — "
                f"look at it to answer: {question}{warn}")
        return {"output": text, "exit_code": 0, "images": _images_payload((b64, mime)),
                "answered_by": "main_model", "measurements": measurements}

    b64, mime = ii.image_to_b64(proc.image)
    prompt = f"{_LITERAL_INSTRUCTIONS}\n\nQuestion: {question}"
    model_override = str(args.get("model") or "").strip() or None
    result = await _ask_vision_model([(_b64_to_bytes(b64), mime)], prompt, owner, model_override)
    text = str(result.get("text") or "")
    model_used = str(result.get("model") or "")
    if not model_used:
        # analyze_image_with_vl_prompt's own bracketed notes ("[No vision
        # model configured ...]", "[Vision is disabled ...]") already say
        # exactly what's wrong and how to fix it (Settings -> Vision).
        return {"output": f"{text}{warn}", "exit_code": 0, "answered_by": "none",
                "measurements": measurements}
    return {"output": f"[{model_used}] {text}{warn}", "exit_code": 0,
            "answered_by": model_used, "measurements": measurements}


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

async def _action_view(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    loaded = _load_image(args)
    pargs = _process_args(args)
    proc = ii.process(loaded.image, grid=args.get("grid"), **pargs)
    measurements = _measurements(loaded, proc)
    warn = _downscale_warning(proc)
    b64, mime = ii.image_to_b64(proc.image)
    ow, oh = proc.output_size
    text = (f"inspect_image view: {loaded.source} — {ow}x{oh}px crop, region "
            f"{measurements['region_used']} of the original ({proc.original_size[0]}x"
            f"{proc.original_size[1]}px). {proc.mapping_note}{warn}")
    return {"output": text, "exit_code": 0, "images": _images_payload((b64, mime)),
            "measurements": measurements}


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------

async def _action_shapes(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    loaded = _load_image(args)
    pargs = _process_args(args)
    result = ii.shapes_pipeline(
        loaded, frame=args.get("frame"), frame_units=args.get("frame_units") or "fraction",
        annotate=bool(args.get("annotate", True)), **pargs,
    )
    images = []
    annotated_mime = result.pop("annotated_mime", "image/png")
    annotated_b64 = result.pop("annotated_b64", None)
    if annotated_b64:
        images = _images_payload((annotated_b64, annotated_mime))
    n = len(result.get("shapes") or [])
    text = f"inspect_image shapes: {loaded.source} — {n} shape(s) found (engine: {result['engine']})."
    if result.get("warning"):
        text += f" {result['warning']}"
    out = {"output": text, "exit_code": 0, "answered_by": "local", "source": loaded.source,
           **result}
    if images:
        out["images"] = images
    return out


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

async def _action_compare(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    owner = ctx.get("owner") if isinstance(ctx, dict) else None
    question = str(args.get("question") or "").strip() or (
        "Are the two marked/cropped objects the same kind of thing? Describe any difference."
    )
    loaded_a = _load_image(args)
    loaded_b = _load_image(args, suffix="_b")
    proc_a = ii.process(loaded_a.image, grid=args.get("grid"), **_process_args(args))
    proc_b = ii.process(loaded_b.image, grid=args.get("grid_b"), **_process_args(args, suffix="_b"))

    img_a, img_b = proc_a.image, proc_b.image
    point = args.get("point")
    if args.get("crosshair") and isinstance(point, (list, tuple)) and len(point) == 2:
        img_a = ii.draw_crosshair(img_a, (float(point[0]), float(point[1])))
        img_b = ii.draw_crosshair(img_b, (float(point[0]), float(point[1])))

    measurements = {"a": _measurements(loaded_a, proc_a), "b": _measurements(loaded_b, proc_b)}
    warn = _downscale_warning(proc_a) or _downscale_warning(proc_b)

    if _main_model_can_see(ctx):
        b64a, mimea = ii.image_to_b64(img_a)
        b64b, mimeb = ii.image_to_b64(img_b)
        text = (f"inspect_image compare: image A ({loaded_a.source}) and image B "
                f"({loaded_b.source}) attached — {question}{warn}")
        return {"output": text, "exit_code": 0, "images": _images_payload((b64a, mimea), (b64b, mimeb)),
                "answered_by": "main_model", "measurements": measurements}

    b64a, mimea = ii.image_to_b64(img_a)
    b64b, mimeb = ii.image_to_b64(img_b)
    prompt = (
        f"{_LITERAL_INSTRUCTIONS}\n\nYou are shown TWO images, A first then B. "
        f"Question: {question}"
    )
    model_override = str(args.get("model") or "").strip() or None
    result = await _ask_vision_model(
        [(_b64_to_bytes(b64a), mimea), (_b64_to_bytes(b64b), mimeb)], prompt, owner, model_override,
    )
    text = str(result.get("text") or "")
    model_used = str(result.get("model") or "")
    if not model_used:
        return {"output": f"{text}{warn}", "exit_code": 0, "answered_by": "none", "measurements": measurements}
    return {"output": f"[{model_used}] {text}{warn}", "exit_code": 0,
            "answered_by": model_used, "measurements": measurements}


# ---------------------------------------------------------------------------
# grid_locate
# ---------------------------------------------------------------------------

async def _action_grid_locate(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    owner = ctx.get("owner") if isinstance(ctx, dict) else None
    question = str(args.get("question") or "").strip()
    if not question:
        raise ii.InspectImageError("inspect_image: grid_locate needs `question` (what to find in the grid)")
    loaded = _load_image(args)
    grid = args.get("grid") or ii.DEFAULT_GRID_DIVISIONS
    proc = ii.process(loaded.image, grid=grid, **_process_args(args))
    if not proc.grid:
        raise ii.InspectImageError("inspect_image: grid_locate could not build a grid")
    cols, rows = proc.grid
    measurements = _measurements(loaded, proc)
    warn = _downscale_warning(proc)
    last_col = ii._column_letter(cols - 1)  # noqa: SLF001 - same module, deliberate reuse
    prompt = (
        f"{_LITERAL_INSTRUCTIONS}\n\nA labelled grid is overlaid on the image: {cols} columns "
        f"(A-{last_col}, left to right) and {rows} rows (1-{rows}, top to bottom). "
        f"Which grid cell(s) contain: {question}? Answer with the cell id(s) only "
        "(e.g. \"C4\" or \"C4, D4\"), or \"unclear\" if none match."
    )

    if _main_model_can_see(ctx):
        b64, mime = ii.image_to_b64(proc.image)
        text = (f"inspect_image grid_locate: {cols}x{rows} grid overlaid on {loaded.source} attached — "
                f"{prompt}{warn}")
        return {"output": text, "exit_code": 0, "images": _images_payload((b64, mime)),
                "answered_by": "main_model", "measurements": measurements,
                "grid_cells": ii.grid_cells(cols, rows)}

    b64, mime = ii.image_to_b64(proc.image)
    model_override = str(args.get("model") or "").strip() or None
    result = await _ask_vision_model([(_b64_to_bytes(b64), mime)], prompt, owner, model_override)
    text = str(result.get("text") or "")
    model_used = str(result.get("model") or "")
    if not model_used:
        return {"output": f"{text}{warn}", "exit_code": 0, "answered_by": "none", "measurements": measurements}
    cell_ids = ii.parse_cell_ids(text, cols, rows)
    matches = []
    for cid in cell_ids:
        idx = ii.cell_id_to_index(cid)
        if idx is None:
            continue
        box = ii.grid_cell_fraction_box(idx[0], idx[1], cols, rows)
        original_box = ii.remap_box_to_original(box, proc)
        matches.append({"cell": cid, "box": list(box),
                         "box_in_original": list(original_box) if original_box else None})
    return {"output": f"[{model_used}] {text}{warn}", "exit_code": 0, "answered_by": model_used,
            "measurements": measurements, "cells": matches}


_ACTIONS = {
    "ask": _action_ask,
    "view": _action_view,
    "shapes": _action_shapes,
    "compare": _action_compare,
    "grid_locate": _action_grid_locate,
}


class InspectImageTool:
    """`inspect_image`: ask/view/shapes/compare/grid_locate — see module
    docstring."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "ask").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return {"error": f"inspect_image: unknown action '{action}' — one of "
                              f"{', '.join(sorted(_ACTIONS))}", "exit_code": 1}
        try:
            return await handler(args, ctx or {})
        except ii.InspectImageError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "inspect_image.invalid"}
        except Exception as exc:  # noqa: BLE001 - a bad image/model call is data, not a crash
            logger.warning("inspect_image(%s) failed: %s", action, exc)
            return {"error": f"inspect_image({action}): {exc}", "exit_code": 1,
                     "error_class": "inspect_image.error"}
