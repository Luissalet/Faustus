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
import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
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


def _vision_max_side_limit() -> int:
    """The most a caller may ask for with max_side when the Vision model
    answers (vision_max_side_limit, 1600 px by default)."""
    try:
        from src.settings import get_setting
        return max(_vision_max_side(), min(int(get_setting("vision_max_side_limit", 1600) or 1600), 4096))
    except Exception:  # noqa: BLE001
        return 1600


def _vision_max_side() -> int:
    try:
        from src.settings import get_setting
        return max(256, min(int(get_setting("vision_max_side", 1280) or 1280), 4096))
    except Exception:  # noqa: BLE001
        return 1280


def _main_model_can_see(ctx: Dict[str, Any]) -> bool:
    """The same check the loop uses to decide whether a tool-result image is
    attached or described: the endpoint's reported capability first
    (`model_supports_vision`), the name only when there is no endpoint.

    The name alone was wrong live: a text-only 27B served by llama.cpp under
    a family name the heuristic counts as multimodal got the raw image
    "attached", which it could not see, instead of the Vision model's answer
    to the question.
    """
    model = str((ctx or {}).get("turn_model") or "").strip()
    if not model:
        return False
    endpoint = str((ctx or {}).get("turn_endpoint_url") or "").strip()
    try:
        if endpoint:
            from src.chat_helpers import model_supports_vision
            return bool(model_supports_vision(model, endpoint))
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
    sees = await asyncio.to_thread(_main_model_can_see, ctx)
    if not sees and pargs.get("max_side"):
        # A caller may ask for more than the default, but not without bound:
        # live (exam run 21) max_side=2400 made every question on a CPU
        # vision model take three and a half minutes instead of one.
        limit = _vision_max_side_limit()
        try:
            if int(pargs["max_side"]) > limit:
                pargs["max_side"] = limit
        except (TypeError, ValueError):
            pargs["max_side"] = _vision_max_side()
    if not sees and not pargs.get("max_side"):
        # What goes to the Vision model is capped (vision_max_side, 1280 px
        # by default): a CPU-only vision model spent up to four minutes per
        # full scanned page. Crop a region for detail, or pass max_side.
        pargs["max_side"] = _vision_max_side()
    proc = ii.process(loaded.image, grid=args.get("grid"), **pargs)
    measurements = _measurements(loaded, proc)
    warn = _downscale_warning(proc)

    if sees:
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
    hint = _transcription_hint(loaded, question, args, text)
    return {"output": f"[{model_used}] {text}{warn}{hint}", "exit_code": 0,
            "answered_by": model_used, "measurements": measurements}


_TRANSCRIBE_Q_RE = re.compile(r"\btranscri|\bliteral|\bword by word|\bpalabra por palabra", re.IGNORECASE)


def _nearby_transcriptions(loaded: "ii.LoadedImage") -> List[str]:
    """Human transcriptions (*transcri*.md/.txt) in the image's folder or its
    parent, one level deep, sorted."""
    try:
        from src.image_inspection import resolve_path
        src_path = resolve_path(str(loaded.source))
    except Exception:  # noqa: BLE001 - a url or an unresolved path: none
        return []
    base = os.path.dirname(src_path)
    found = []
    for root in {base, os.path.dirname(base)}:
        try:
            for dirpath, _dirs, files in os.walk(root):
                if dirpath.count(os.sep) - root.count(os.sep) > 1:
                    continue
                for name in files:
                    low = name.lower()
                    if ("transcri" in low or "transcript" in low) and low.endswith((".md", ".txt")):
                        found.append(os.path.join(dirpath, name))
        except Exception:  # noqa: BLE001
            continue
    return sorted(set(found))


_QUOTED_TERM_RE = re.compile(r"[\"\u201c\u00ab']([^\"\u201d\u00bb'\n]{2,40})[\"\u201d\u00bb']")
_CAP_WORD_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z\u00c0-\u00dd][a-z\u00e0-\u00ff]{3,})\b")


def _matching_transcription_lines(path: str, question: str, answer: str, limit: int = 8) -> List[str]:
    """Lines of the transcription that contain a term the question quotes
    (or a capitalised name it uses mid-sentence) or one the vision answer
    quotes. A term that matches more than 3 lines is too common to point
    at anything and is dropped."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip() for ln in fh.read(200_000).splitlines()]
    except OSError:
        return []
    terms = {t.strip() for t in _QUOTED_TERM_RE.findall(question or "") + _QUOTED_TERM_RE.findall(answer or "")}
    terms |= set(_CAP_WORD_RE.findall(question or ""))
    picked: List[str] = []
    for term in sorted(t for t in terms if len(t) >= 3):
        rx = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        hits = [ln.strip() for ln in lines if ln.strip() and rx.search(ln)]
        if not hits or len(hits) > 3:
            continue
        for h in hits:
            if h not in picked:
                picked.append(h)
    return picked[:limit]


def _transcription_hint(loaded: "ii.LoadedImage", question: str, args: Dict[str, Any],
                        answer: str = "") -> str:
    """Point the model at the human transcription next to the image.

    Live (exam runs 13-21) a text-only model re-transcribed, crop by crop, a
    page whose transcription it had already read — minutes per crop on a
    CPU vision model, whose reading is the worse of the two. In run 24 it
    went further: the vision model miscounted the arrows of a line, and the
    deliverable declared the human transcription wrong. So when the
    transcription has the lines the question is about, they are quoted
    right under the vision answer; for any other transcription-like
    question the plain pointer is given."""
    if str(args.get("action") or "ask").lower() != "ask":
        return ""
    found = _nearby_transcriptions(loaded)
    if not found:
        return ""
    base = os.path.dirname(os.path.dirname(found[0]))
    rel = os.path.relpath(found[0], base) if base else found[0]
    lines = _matching_transcription_lines(found[0], question, answer)
    if lines:
        quoted = "\n".join(f"  {ln[:200]}" for ln in lines)
        return (f"\n\n[inspect_image: the human transcription ({rel}) has these lines for what "
                f"you asked:\n{quoted}\nWhere the vision reading above differs, trust the "
                "transcription for words and for counts of written signs (arrows, marks, letters); "
                "use the vision model only for what the transcription leaves out.]")
    if not _TRANSCRIBE_Q_RE.search(question or ""):
        return ""
    return (f"\n\n[inspect_image: a human transcription exists ({rel}). The vision model's reading "
            "above is less reliable than it for text; rely on the transcription for wording, and "
            "ask inspect_image action=\"unlisted\" with text_path for only what it leaves out "
            "(marks, circles, symbols, positions).]")


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

async def _action_view(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    if (ctx or {}).get("turn_model") and not await asyncio.to_thread(_main_model_can_see, ctx):
        # The turn's model cannot see the crop it would be handed; the loop
        # would caption it with a generic prompt. Seen live: a text-only 27B
        # called `view` over and over on a scanned page, each time getting a
        # general description instead of an answer. Ask the Vision model the
        # literal question instead, and say how to do better.
        # The size a blind model asks for in `view` is meant for its own
        # eyes, not for the Vision model: live, max_side=2400 on a full page
        # kept a CPU-only vision model busy for over nine minutes.
        capped = min(int(args.get("max_side") or _vision_max_side()), _vision_max_side())
        out = await _action_ask({**args, "max_side": capped, "question": str(args.get("question") or "").strip() or (
            "Transcribe every piece of text in this region exactly, then list every drawing, "
            "mark, circle, arrow or symbol with its position as fractions of the image.")}, ctx)
        out["output"] = (
            "(Your model cannot see images, so `view` was answered by the Vision model. "
            "Ask it specific questions with action=\"ask\" and a small `region` for details; "
            "use action=\"shapes\" to measure where marks are.)\n" + str(out.get("output") or "")
        )
        return out
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

    if await asyncio.to_thread(_main_model_can_see, ctx):
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

    if await asyncio.to_thread(_main_model_can_see, ctx):
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


_UNLISTED_PROMPT = (
    "Below is a text transcription of this image, made by someone else. Your job "
    "is the DIFFERENCE: list every visible element that the transcription does "
    "NOT capture, or gets wrong. Think of hand-drawn marks (circles, ellipses, "
    "arrows, underlines, crosses), drawings and figures, symbols and signs "
    "(including unusual numerals or glyphs), numbers, stamps, and any text it "
    "omits or misreads. For each element say what it is, where it is (fractions "
    "of the image), and what it touches or points at. Do not repeat what the "
    "transcription already says correctly.\n\nTRANSCRIPTION:\n"
)


async def _action_unlisted(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """What the image shows that a given transcription leaves out -- the
    check a transcription invites ("drawings and signs not transcribed must
    be read in the original"). One question instead of re-transcribing the
    whole page region by region, which is what a text-only model did live."""
    text = str(args.get("text") or "").strip()
    text_path = str(args.get("text_path") or "").strip()
    if not text and text_path:
        # Same workspace-confined resolution the image path gets.
        resolved = str(ii.resolve_path(text_path))
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise ii.InspectImageError(f"inspect_image: cannot read text_path {text_path!r}: {exc}")
    if not text:
        raise ii.InspectImageError(
            "inspect_image unlisted: give the transcription as `text` or `text_path`")
    question = _UNLISTED_PROMPT + text[:12000]
    extra = str(args.get("question") or "").strip()
    if extra:
        question += f"\n\nAlso: {extra}"
    return await _action_ask({**args, "question": question}, ctx)


_ACTIONS = {
    "ask": _action_ask,
    "unlisted": _action_unlisted,
    "view": _action_view,
    "shapes": _action_shapes,
    "compare": _action_compare,
    "grid_locate": _action_grid_locate,
}


# ---------------------------------------------------------------------------
# repeat ledger
# ---------------------------------------------------------------------------
#
# Exam runs showed a text-only model asking the Vision model the SAME
# question about the SAME crop two, three, four times in a row (the second
# answer then comes from the vision cache in a second, identical to the
# first). The loop breaker only escalates after several identical calls,
# and by then the turn has burnt rounds re-reading one paragraph. The
# ledger below remembers, per session, which exact (action, path, region,
# zoom, question...) the model already asked and says so in the result:
# the second time with the answer still attached, from the third time on
# with only its start, so re-asking stops paying off and the model moves to
# the next open step of its plan.

_REPEAT_LEDGER: "OrderedDict[Tuple[str, str], int]" = OrderedDict()
_REPEAT_LEDGER_MAX = 512
_REPEAT_EXCERPT_CHARS = 400


def _repeat_key(args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    session = str((ctx or {}).get("session_id") or "").strip()
    if not session:
        return None
    try:
        norm = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - unserializable args just skip the ledger
        return None
    return session, hashlib.sha256(norm.encode("utf-8")).hexdigest()


#: Per session: how many calls IN A ROW were exact repeats. Live (exam run
#: 14) a model that got the repeat note went round the same seven questions
#: again and again — a cycle too long for the loop breaker's periods. The
#: agent loop reads this (``consecutive_repeats``) and pauses the evidence
#: tools once it reaches a few.
_CONSECUTIVE_REPEATS: "OrderedDict[str, int]" = OrderedDict()


def consecutive_repeats(session_id: Optional[str]) -> int:
    return _CONSECUTIVE_REPEATS.get(str(session_id or ""), 0)


def reset_consecutive_repeats(session_id: Optional[str]) -> None:
    _CONSECUTIVE_REPEATS.pop(str(session_id or ""), None)


def _note_repeat(key: Optional[Tuple[str, str]]) -> int:
    """Record one more call for ``key``; return how many times it was seen
    INCLUDING this call (1 = first time)."""
    if key is None:
        return 1
    count = _REPEAT_LEDGER.pop(key, 0) + 1
    _REPEAT_LEDGER[key] = count
    while len(_REPEAT_LEDGER) > _REPEAT_LEDGER_MAX:
        _REPEAT_LEDGER.popitem(last=False)
    session = key[0]
    streak = _CONSECUTIVE_REPEATS.pop(session, 0) + 1 if count >= 2 else 0
    if not streak:
        _CONSECUTIVE_REPEATS.pop(session, None)
    else:
        _CONSECUTIVE_REPEATS[session] = streak
        while len(_CONSECUTIVE_REPEATS) > _REPEAT_LEDGER_MAX:
            _CONSECUTIVE_REPEATS.popitem(last=False)
    return count


def _repeat_note(count: int) -> str:
    return (f"[inspect_image: you already made this EXACT call {count - 1} time(s) in "
            "this session — same image, region and question — so the answer cannot "
            "change. Do not ask it again. Use what you already have: write down the "
            "facts it gives, then move to the next open step of your plan (a "
            "different region, a different question, a calculation, or the answer "
            "itself).]")


def _with_repeat_note(result: Dict[str, Any], count: int) -> Dict[str, Any]:
    if count < 2 or not isinstance(result, dict) or result.get("error"):
        return result
    out = str(result.get("output") or "")
    if count >= 3 and len(out) > _REPEAT_EXCERPT_CHARS:
        out = out[:_REPEAT_EXCERPT_CHARS].rstrip() + " … [rest identical to your earlier call]"
    result = dict(result)
    result["output"] = f"{_repeat_note(count)}\n\n{out}"
    result["repeat_count"] = count
    return result


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
        count = _note_repeat(_repeat_key({**args, "action": action}, ctx or {}))
        try:
            return _with_repeat_note(await handler(args, ctx or {}), count)
        except ii.InspectImageError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "inspect_image.invalid"}
        except Exception as exc:  # noqa: BLE001 - a bad image/model call is data, not a crash
            logger.warning("inspect_image(%s) failed: %s", action, exc)
            return {"error": f"inspect_image({action}): {exc}", "exit_code": 1,
                     "error_class": "inspect_image.error"}
