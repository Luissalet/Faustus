"""agent_tools/loop_card_tools.py -- `loop_card_create`, `loop_card_check`, `loop_card_list`,
`loop_card_get`, `loop_card_delete`: thin executors over `src.loop_card.run`.

Creating or checking a card runs the measuring command N times in the folder given by
`cwd` (default: the turn's active workspace), so both are classed as code execution in
tool_capabilities; list/get/delete only touch the stored cards.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from src import loop_card


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


def _make(tool: str, action: str):
    class _Tool:
        async def execute(self, content: Any, ctx: dict) -> dict:
            args = _args(content)
            if not args and action != "list":
                return {"error": f"{tool}: the arguments must be a JSON object", "exit_code": 1}
            owner = str((ctx or {}).get("owner") or "")
            if not args.get("project") and (ctx or {}).get("project_id"):
                args["project"] = str(ctx["project_id"])
            if action in ("create",) and not args.get("cwd"):
                try:
                    from src.tool_execution import get_active_workspace
                    args["cwd"] = get_active_workspace() or None
                except Exception:  # noqa: BLE001
                    pass
            data = await asyncio.to_thread(loop_card.run, owner, {**args, "action": action})
            if data.get("error"):
                return {"error": f"{tool}: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}
            return {"output": loop_card.render(data), "exit_code": 0, "result": data}
    _Tool.__name__ = "".join(p.capitalize() for p in tool.split("_")) + "Tool"
    return _Tool


LoopCardCreateTool = _make("loop_card_create", "create")
LoopCardCheckTool = _make("loop_card_check", "check")
LoopCardListTool = _make("loop_card_list", "list")
LoopCardGetTool = _make("loop_card_get", "get")
LoopCardDeleteTool = _make("loop_card_delete", "delete")

HANDLERS = {
    "loop_card_create": LoopCardCreateTool, "loop_card_check": LoopCardCheckTool, "loop_card_list": LoopCardListTool,
    "loop_card_get": LoopCardGetTool, "loop_card_delete": LoopCardDeleteTool,
}
