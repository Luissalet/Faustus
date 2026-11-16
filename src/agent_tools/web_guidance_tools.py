"""agent_tools/web_guidance_tools.py -- the `web_guidance` tool (src/web_guidance.py).

    action=search    ranked guides for a short capability query ("light dismiss dialog")
    action=retrieve  the text of one or more guides by id, in slices when a guide is long
    action=list      every guide, or one category's, with the vendored version

The guides are files shipped with the app; nothing here uses the network.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from src import web_guidance


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
    return {"query": raw} if raw else {}


def _fail(message: str) -> Dict[str, Any]:
    return {"error": f"web_guidance: {message}", "error_class": "web_guidance.bad_args", "exit_code": 1}


class WebGuidanceTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        action = str(a.get("action") or ("retrieve" if a.get("ids") or a.get("id") else "search")).strip().lower()
        if action == "search":
            query = str(a.get("query") or a.get("q") or "").strip()
            if not query:
                return _fail("`query` is required (2-5 words naming one web platform capability)")
            result = web_guidance.search(query, a.get("limit") or web_guidance.DEFAULT_LIMIT, a.get("category"))
        elif action == "retrieve":
            ids = a.get("ids") or a.get("id")
            if not ids:
                return _fail("`ids` is required (a guide id, or several separated by commas)")
            result = web_guidance.retrieve(ids, a.get("offset") or 0, a.get("max_chars") or web_guidance.DEFAULT_MAX_CHARS)
        elif action == "list":
            result = {**web_guidance.list_guides(a.get("category")), "vendored": web_guidance.status()}
        else:
            return _fail("`action` is search, retrieve or list")
        return {"output": web_guidance.render(action, result), "exit_code": 0, "result": result}


HANDLERS = {"web_guidance": WebGuidanceTool}
