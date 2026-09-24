"""agent_tools/fix_memory_tools.py — the `recall_fixes` tool.

Read-only, no side effects: looks up past solved issues in this project's
fix memory (`src/fix_memory.py`) so the agent can reuse a working solution
instead of rediscovering it. Same call shape as the git read tools
(`content: str, ctx: dict`) — see `src/agent_tools/git_tools.py`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

__all__ = ["RecallFixesTool"]


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


class RecallFixesTool:
    """`recall_fixes`: past fixes in the current project that look similar
    to `query` (and, optionally, the given `files`/`error`). Read-only —
    never records or changes anything."""

    async def execute(self, content: str, ctx: dict = None) -> dict:
        from src import fix_memory

        ctx = ctx or {}
        args = _args(content)
        owner = str(ctx.get("owner") or "") or None
        workspace = ctx.get("workspace")
        project_id = ctx.get("project_id")
        project = fix_memory.project_key(workspace, project_id)

        query = str(args.get("query") or "").strip()
        files = args.get("files")
        files = [str(f) for f in files] if isinstance(files, list) else None
        error = args.get("error")
        errors = [str(error)] if error else None
        try:
            k = max(1, min(int(args.get("k") or 5), 20))
        except (TypeError, ValueError):
            k = 5

        try:
            items = fix_memory.recall(owner, project, query, files=files, errors=errors, k=k)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"recall_fixes: {exc}", "exit_code": 1}

        if not items:
            return {"output": "No similar past fixes found.", "exit_code": 0, "count": 0, "fixes": []}

        lines = []
        for it in items:
            files_bit = f" (files: {', '.join(it.get('files') or [])})" if it.get("files") else ""
            lines.append(f"- [{it.get('outcome')}] {it.get('task', '')} -> {it.get('solution', '')}{files_bit}")
        return {
            "output": "\n".join(lines),
            "exit_code": 0,
            "count": len(items),
            "fixes": items,
        }
