"""agent_tools/annotation_tools.py -- annotate and review beside the chat (src/annotations.py).

    annotate_open     open the Annotate tab on a target: this chat's plan, a workspace file, a Studio document
                      or a local web page; the person selects, comments and sends the notes back as a message
    annotations_list  the notes the person left in this chat (pending first), quoting what each is about
    review_open       the workspace's uncommitted changes, or its diff against a branch/ref, as a review with
                      per-line notes in the Annotate tab
    review_guide      the review ordered into chapters (core, effects, glue) with a short summary each
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

from src import annotations, surfaces


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {"ref": raw} if raw else {}


def _fail(tool: str, code: str, message: str) -> Dict[str, Any]:
    return {"error": f"{tool}: {message}", "error_class": "annotations." + code, "exit_code": 1}


def _session(ctx: dict) -> str:
    return surfaces.top_session((ctx or {}).get("session_id"))


def _target(a: Dict[str, Any], ctx: dict) -> Dict[str, Any]:
    kind = str(a.get("kind") or "").strip().lower()
    ref = str(a.get("ref") or a.get("path") or a.get("url") or "").strip()
    if not kind:
        kind = "web" if ref.startswith("http") or ref.lstrip(":").isdigit() else ("file" if ref else "plan")
    workspace = str((ctx or {}).get("workspace") or a.get("workspace") or "")
    if kind == "file" and ref and not os.path.isabs(ref) and workspace:
        ref = os.path.join(workspace, ref)
    if kind == "plan" and not ref:
        ref = "last"
    tgt: Dict[str, Any] = {"kind": kind, "ref": ref, "title": str(a.get("title") or "")}
    if workspace:
        tgt["workspace"] = workspace
    return tgt


class AnnotateOpenTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        sid = _session(ctx)
        if not sid:
            return _fail("annotate_open", "bad_args", "this call has no conversation to open the Annotate tab in")
        from routes.annotation_routes import open_target
        try:
            res = await asyncio.to_thread(open_target, sid, _target(a, ctx))
        except annotations.AnnotationError as exc:
            return _fail("annotate_open", exc.code, exc.message)
        tgt = res["target"]
        text = (f"The Annotate tab beside the chat shows {tgt['kind']} {tgt['title']!r}. The person selects parts, "
                "comments (comment, suggested change, delete, question) and sends the notes back as their next message; "
                "annotations_list reads them at any time.")
        if res.get("web"):
            text += f" The page is proxied live at {res['web']['url']} (hot reload works)."
        if not surfaces.has_host(sid):
            text += " No Studio window is showing this chat right now: the tab opens when the person comes back."
        return {"output": text, "exit_code": 0, "result": res}


class AnnotationsListTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        sid = _session(ctx)
        if not sid:
            return _fail("annotations_list", "bad_args", "this call has no conversation")
        pending_only = bool(a.get("pending_only"))
        try:
            notes = await asyncio.to_thread(annotations.list_, sid, None, pending_only)
            if not notes:
                return {"output": "No annotations in this chat" + (" waiting to be sent." if pending_only else "."),
                        "exit_code": 0, "result": {"annotations": []}}
            res = await asyncio.to_thread(annotations.compose, sid, [n["id"] for n in notes], None, False, True)
        except annotations.AnnotationError as exc:
            return _fail("annotations_list", exc.code, exc.message)
        pending = sum(1 for n in notes if not n.get("sent_at"))
        head = f"{len(notes)} annotations ({pending} not sent yet)."
        return {"output": head + "\n\n" + res["text"], "exit_code": 0, "result": {"annotations": notes}}


class ReviewOpenTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        sid = _session(ctx)
        workspace = str(a.get("workspace") or (ctx or {}).get("workspace") or "").strip()
        base = str(a.get("base") or a.get("ref") or "HEAD").strip() or "HEAD"
        try:
            res = await asyncio.to_thread(annotations.open_review, sid, workspace, base, str(a.get("diff") or ""))
        except annotations.AnnotationError as exc:
            return _fail("review_open", exc.code, exc.message)
        if sid:
            surfaces.emit(sid, "open", {"kind": "anotar", "target": {"kind": "diff", "ref": f"{res['workspace']}@{base}",
                                                                      "title": f"Cambios frente a {base}"}})
        st = res["stats"]
        lines = [f"Review open beside the chat: {st['files']} files, +{st['added']} -{st['removed']}, {st['hunks']} hunks "
                 f"against {base}" + (" (diff truncated)" if res.get("truncated") else "") + "."]
        for f in res["files"][:60]:
            lines.append(f"- {f['path']} ({f['status']}, +{f['added']} -{f['removed']})")
        lines.append("The person comments per line in the Annotate tab; review_guide orders the hunks into chapters.")
        return {"output": "\n".join(lines), "exit_code": 0, "result": {"stats": st, "files": [f["path"] for f in res["files"]]}}


class ReviewGuideTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        sid = _session(ctx)
        if not sid:
            return _fail("review_guide", "bad_args", "this call has no conversation")
        try:
            g = await annotations.guide(sid, owner=str((ctx or {}).get("owner") or "") or None, model=str(a.get("model") or ""))
            rv = await asyncio.to_thread(annotations.current_review, sid)
        except annotations.AnnotationError as exc:
            return _fail("review_guide", exc.code, exc.message)
        surfaces.emit(sid, "meta", {"kind": "anotar", "guide": len(g["chapters"])})
        return {"output": annotations.render_guide(g, (rv or {}).get("files") or []), "exit_code": 0, "result": g}
