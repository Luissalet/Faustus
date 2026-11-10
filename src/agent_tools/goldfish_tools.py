"""agent_tools/goldfish_tools.py -- `goldfish_review`: draft with an elephant, review with goldfish
(`src.goldfish`): staged review, sharpening loop, draft-and-review, brainstorm, blind multi-model
panel and document/code drift, run by fresh workers that never see the conversation."""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from src import goldfish


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
    return {}


class GoldfishReviewTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        owner = str((ctx or {}).get("owner") or "")
        path = str(args.get("path") or "").strip()
        if path and not path.startswith("brain:") and not os.path.isabs(path):
            try:
                from src.tool_execution import get_active_workspace
                ws = get_active_workspace()
                if ws:
                    args["path"] = os.path.join(ws, path)
            except Exception:  # noqa: BLE001
                pass
        data = await goldfish.run_action(owner, args)
        if data.get("error"):
            return {"error": f"goldfish_review: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}
        return {"output": goldfish.render(data), "exit_code": 0}


HANDLERS = {"goldfish_review": GoldfishReviewTool}
