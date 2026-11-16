"""
annotations_server.py

MCP server for annotations and guided review (`src/annotations.py`): the chat's tools `annotate_open`,
`annotations_list`, `review_open` and `review_guide`, plus `annotation_add` and `annotations_compose` so an
agent outside the chat (a coding agent, a scheduled task) can leave notes and read them as one message.
Annotations live in the running Faustus (the Studio window showing the chat shows the Annotate tab), so this
server is a thin client of its `/api/annotations/*` routes; every tool takes the `session` (chat id).

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is used.
* `FAUSTUS_INTERNAL_API_URL` / `FAUSTUS_URL` (default: the local server), `FAUSTUS_TOKEN` / `FAUSTUS_TOKEN_FILE`
  and `FAUSTUS_INTERNAL_TOOL_TOKEN` authenticate the calls, like the other built-in servers.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("annotations")
_NAMES = ("annotate_open", "annotations_list", "review_open", "review_guide")
_SESSION = {"type": "string", "description": "Chat session id whose Annotate tab is meant"}
_TARGET = {"type": "object", "description": "{kind: plan|file|doc|web|diff, ref, title?}"}
_EXTRA = {
    "annotation_add": {
        "description": "Leave an annotation in a chat on a quoted part of a target: type comment, suggestion (text = the replacement), delete or question. For a review line, target {kind: diff} and anchor {file, line, side: new|old}.",
        "parameters": {"type": "object", "properties": {
            "target": _TARGET,
            "type": {"type": "string", "enum": ["comment", "suggestion", "delete", "question"]},
            "quote": {"type": "string", "description": "The exact text the note is about"},
            "text": {"type": "string", "description": "The comment, the replacement or the question"},
            "anchor": {"type": "object", "description": "Optional: {selector} for a web element, {file, line, side} for a diff line"}},
            "required": ["target", "type"]}},
    "annotations_compose": {
        "description": "The chat's pending annotations as the one user message Studio sends to the agent, quoting what each is about. mark_sent=true marks them as sent.",
        "parameters": {"type": "object", "properties": {
            "mark_sent": {"type": "boolean", "description": "Mark the composed annotations as sent (default false)"}}}},
}


def _schemas() -> dict:
    from src.annotation_schemas import ANNOTATION_TOOL_SCHEMAS
    out = {}
    for s in ANNOTATION_TOOL_SCHEMAS:
        fn = s["function"]
        out[fn["name"]] = {"description": fn["description"], "parameters": json.loads(json.dumps(fn["parameters"]))}
    out.update(json.loads(json.dumps(_EXTRA)))
    for spec in out.values():
        params = spec["parameters"]
        params["properties"] = {"session": _SESSION, **params.get("properties", {})}
        params["required"] = ["session", *params.get("required", [])]
    return out


@server.list_tools()
async def list_tools() -> list[Tool]:
    schemas = _schemas()
    return [Tool(name=n, description=s["description"], inputSchema=s["parameters"]) for n, s in schemas.items()]


def _headers() -> dict:
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        try:
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if token:
        headers["Authorization"] = "Bearer " + token
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    return headers


def _render(name: str, payload: dict) -> str:
    from src import annotations
    if name == "review_open" and payload.get("files") is not None:
        st = payload.get("stats") or {}
        lines = [f"Review: {st.get('files', 0)} files, +{st.get('added', 0)} -{st.get('removed', 0)}, "
                 f"{st.get('hunks', 0)} hunks against {payload.get('base')}."]
        lines += [f"- {f['path']} ({f['status']}, +{f['added']} -{f['removed']})" for f in payload["files"][:80]]
        return "\n".join(lines)
    if name == "review_guide" and payload.get("chapters") is not None:
        return annotations.render_guide(payload, payload.get("_files") or [])
    if name in ("annotations_list", "annotations_compose") and "text" in payload:
        return payload["text"] or "No annotations."
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    session = str(args.pop("session", "") or "").strip()
    if name not in (*_NAMES, *_EXTRA) or not session:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Invalid annotations request: `session` and a known tool are required")])
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    h = _headers()
    try:
        async with httpx.AsyncClient(timeout=240, trust_env=False) as client:
            if name == "annotate_open":
                kind = str(args.get("kind") or "plan")
                ref = str(args.get("ref") or ("last" if kind == "plan" else ""))
                r = await client.post(base + "/api/annotations/open", headers=h,
                                      json={"session": session, "target": {"kind": kind, "ref": ref, "title": args.get("title") or ""}})
            elif name == "annotations_list":
                r = await client.post(base + "/api/annotations/compose", headers=h,
                                      json={"session": session, "mark_sent": False,
                                            "include_sent": args.get("pending_only") is not True})
            elif name == "annotations_compose":
                r = await client.post(base + "/api/annotations/compose", headers=h,
                                      json={"session": session, "mark_sent": bool(args.get("mark_sent"))})
            elif name == "annotation_add":
                r = await client.post(base + "/api/annotations", headers=h, json={"session": session, "author": "agent", **args})
            elif name == "review_open":
                r = await client.post(base + "/api/annotations/review", headers=h,
                                      json={"session": session, "workspace": args.get("workspace") or "",
                                            "base": args.get("base") or "HEAD"})
            else:
                r = await client.post(base + "/api/annotations/review/guide", headers=h,
                                      json={"session": session, "model": args.get("model") or ""})
                if r.is_success:
                    payload = r.json()
                    rv = await client.get(base + "/api/annotations/review", headers=h, params={"session": session})
                    payload["_files"] = rv.json().get("files") if rv.is_success else []
                    return CallToolResult(isError=False, content=[TextContent(type="text", text=_render(name, payload))])
            payload, error = r.json(), not r.is_success
    except (httpx.HTTPError, ValueError) as exc:
        payload, error = {"error": f"Faustus did not answer ({type(exc).__name__})"}, True
    return CallToolResult(isError=error, content=[TextContent(type="text", text=_render(name, payload))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
