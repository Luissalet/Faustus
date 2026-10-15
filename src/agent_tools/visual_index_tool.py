"""agent_tools/visual_index_tool.py -- the visual index as agent tools.

``visual_index_add`` / ``visual_search`` / ``view_tile`` / ``visual_index_list`` / ``visual_index_remove``: thin wrappers over
``src.visual_index.service`` (the same service the REST routes and the MCP server call). Search returns strips (a piece of a
page up to 1024 px high) with source, page, y offset, score and the path of the strip image; ``view_tile`` shows ONE strip to
the model: attached as an image when the model running the turn can see images, otherwise read by the resident vision model
(with the caller's question), and when there is no vision model at all the result says so instead of describing anything.
At most ``visual_index_view_limit_per_turn`` strips are shown per turn.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional

from src.visual_index import service as svc
from src.visual_index.embedder import EmbedderUnavailable

_ADD_KEYS = frozenset({"paths", "urls", "sources", "collection", "pages", "force"})


def _args(content: Any) -> Dict[str, Any]:
    raw = content.strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _owner(ctx: Any) -> Optional[str]:
    return ctx.get("owner") if isinstance(ctx, dict) else None


def _as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _fail(tool: str, exc: Exception) -> Dict[str, Any]:
    code = getattr(exc, "code", "error")
    return {"error": f"{tool}: {exc}", "error_code": code, "exit_code": 1}


def _turn_key(ctx: Any) -> str:
    if not isinstance(ctx, dict):
        return "-"
    return str(ctx.get("turn_id") or ctx.get("run_id") or ctx.get("session_id") or "-")


def _line(h: Dict[str, Any], n: int) -> str:
    where = f"p.{h['page']}" if h.get("kind") == "pdf" else (f"y={h['y_offset']}px" if h.get("kind") == "web" else "")
    extra = f" y={h['y_offset']}px" if h.get("kind") == "pdf" and h.get("y_offset") else ""
    name = h.get("title") or h["source"]
    return f"{n}. {h['score']:.3f}  {name} {where}{extra}  [{h['source']}]  id={h['id']}"


class VisualIndexAddTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        if set(args) - _ADD_KEYS:
            return {"error": 'visual_index_add: expected {"paths"|"urls": [...], "collection"?: "default", "pages"?: "1-5", "force"?: false}',
                    "error_code": "invalid_arguments", "exit_code": 1}
        sources = _as_list(args.get("paths")) + _as_list(args.get("urls")) + _as_list(args.get("sources"))
        try:
            result = await asyncio.to_thread(svc.get_service().add, _owner(ctx), sources,
                                             collection=str(args.get("collection") or "default"), pages=args.get("pages"),
                                             force=bool(args.get("force")))
        except EmbedderUnavailable as exc:
            return _fail("visual_index_add", exc)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("visual_index_add", exc)
        lines = [f"Collection {result['collection']}:"]
        for r in result["results"]:
            if not r.get("ok"):
                lines.append(f"- {r['source']}: FAILED ({r.get('error')})")
            elif r.get("unchanged"):
                lines.append(f"- {r['source']}: unchanged ({r['total']} strips)")
            else:
                lines.append(f"- {r['source']}: {r['total']} strips ({r['added']} embedded, {r['reused']} reused, {r['removed']} removed)"
                             + (f"; {'; '.join(r['notes'])}" if r.get("notes") else ""))
        return {"output": "\n".join(lines), "result": result, "exit_code": 0}


class VisualSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        query = str(args.get("query") or "").strip()
        if not query or set(args) - {"query", "k", "collection"}:
            return {"error": 'visual_search: expected {"query": "...", "k"?: 5, "collection"?: "name"}',
                    "error_code": "invalid_arguments", "exit_code": 1}
        try:
            result = await asyncio.to_thread(svc.get_service().search, _owner(ctx), query, k=args.get("k") or 5,
                                             collection=args.get("collection") or None)
        except EmbedderUnavailable as exc:
            return _fail("visual_search", exc)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("visual_search", exc)
        hits = result["results"]
        if not hits:
            return {"output": result.get("note") or "No strips matched.", "results": [], "exit_code": 0}
        text = "\n".join(_line(h, i) for i, h in enumerate(hits, 1))
        text += "\nLook at a strip with view_tile {\"id\": ...}; a strip is a piece of the page, not its text."
        return {"output": text, "results": hits, "collections": result["collections"], "exit_code": 0}


class ViewTileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        sid = str(args.get("id") or "").strip()
        if not sid or set(args) - {"id", "question"}:
            return {"error": 'view_tile: expected {"id": "collection~0123456789ab", "question"?: "what to read in it"}',
                    "error_code": "invalid_arguments", "exit_code": 1}
        service = svc.get_service()
        try:
            tile = await asyncio.to_thread(service.get_tile, _owner(ctx), sid)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("view_tile", exc)
        remaining = svc.take_view_slot(_turn_key(ctx))
        if remaining is None:
            return {"error": f"view_tile: at most {svc.view_limit()} strips can be viewed in one turn; refine the search or "
                             f"continue in the next turn", "error_code": "view_limit", "exit_code": 1}
        where = f"{tile['source']} page {tile['page']}, {tile['width']}x{tile['height']}px at y={tile['y_offset']}"
        from src.agent_tools.image_inspect_tool import _main_model_can_see

        with open(tile["path"], "rb") as f:
            raw = f.read()
        if await asyncio.to_thread(_main_model_can_see, ctx):
            return {"output": f"view_tile: strip {tile['id']} attached ({where}). Look at it to answer"
                              + (f": {args['question']}" if args.get("question") else "."),
                    "images": [{"data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"}],
                    "tile": tile, "answered_by": "main_model", "views_left": remaining, "exit_code": 0}
        question = str(args.get("question") or "").strip()
        try:
            read = await asyncio.to_thread(service.read_tile, _owner(ctx), sid, question)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("view_tile", exc)
        if read["answered_by"] == "none":
            return {"output": f"view_tile: strip {tile['id']} ({where}) exists but nobody can look at it now: the model of this turn "
                              f"cannot see images and {read['text']}. Nothing was read from it; do not describe it.",
                    "tile": tile, "answered_by": "none", "vision_available": False, "views_left": remaining, "exit_code": 0}
        return {"output": f"[{read['answered_by']}] {read['text']}", "tile": tile, "answered_by": read["answered_by"],
                "views_left": remaining, "exit_code": 0}


def _status_text(status: Dict[str, Any]) -> str:
    emb = status.get("embedder") or {}
    parts = [f"Embedder {emb.get('embedder', '?')}: mode {emb.get('mode', '?')}, "
             + ("loaded" if emb.get("loaded") else "not loaded")
             + (f" on {emb['device']}" if emb.get("device") else "")]
    if emb.get("mode") == "subprocess":
        parts.append(f"python {emb.get('python') or '(empty)'}" + ("" if emb.get("python_exists") else " (NOT FOUND)"))
        info = emb.get("python_info")
        if info:
            parts.append(f"torch {info.get('torch')} cuda {info.get('cuda')} cards {info.get('device_count')} transformers {info.get('transformers')}")
        if emb.get("problem"):
            parts.append(f"problem: {emb['problem']}")
    elif emb.get("dependencies_missing"):
        parts.append(f"missing {', '.join(emb['dependencies_missing'])}: {emb.get('install_hint')}")
    parts.append(f"model downloaded: {emb.get('model_downloaded')}; allowed GPUs {emb.get('allowed_gpus')}")
    vision = status.get("vision") or {}
    parts.append("vision model: " + (str(vision.get("model")) if vision.get("available") else f"none ({vision.get('note', '')})"))
    return "\n".join(parts)


class VisualIndexListTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        if set(args) - {"collection", "status", "probe"}:
            return {"error": 'visual_index_list: expected {"collection"?: "name"} or {"status": true, "probe"?: true}',
                    "error_code": "invalid_arguments", "exit_code": 1}
        if args.get("status") or args.get("probe"):
            status = await asyncio.to_thread(svc.get_service().status, _owner(ctx), probe=bool(args.get("probe")))
            return {"output": _status_text(status), "status": status, "exit_code": 0}
        try:
            result = await asyncio.to_thread(svc.get_service().list, _owner(ctx), args.get("collection") or None)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("visual_index_list", exc)
        if "collections" in result:
            rows = result["collections"]
            text = "\n".join(f"- {c['collection']}: {c['sources']} sources, {c['strips']} strips ({c['embedder']})" for c in rows) \
                if rows else "The visual index has no collections yet."
        else:
            text = f"Collection {result['collection']['collection']}:\n" + "\n".join(
                f"- {s['source']} ({s.get('kind')}, {s.get('strips')} strips, {s.get('pages')} pages)" for s in result["sources"])
        return {"output": text, **result, "exit_code": 0}


class VisualIndexRemoveTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        if not args.get("collection") or set(args) - {"collection", "source"}:
            return {"error": 'visual_index_remove: expected {"collection": "name", "source"?: "one path or URL (omit to delete the whole collection)"}',
                    "error_code": "invalid_arguments", "exit_code": 1}
        try:
            result = await asyncio.to_thread(svc.get_service().remove, _owner(ctx), str(args["collection"]), args.get("source") or None)
        except (svc.VisualIndexError, ValueError) as exc:
            return _fail("visual_index_remove", exc)
        what = f"source {result['source']}" if result.get("source") else "the whole collection"
        return {"output": f"Removed {what} from {result['collection']} ({result['removed_strips']} strips).", **result, "exit_code": 0}
