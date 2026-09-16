"""``run_code`` native tool (T6, A10/A11): Code Mode.

Lets the model write a short Python program that composes several Faustus
tool calls (``tools.call("read_file", {...})``, etc.) in one round instead
of one model round trip per tool call. The program runs isolated in a
subprocess (``src/code_mode/runner.py``); every ``tools.call`` it makes is
dispatched through the SAME ``src.tool_execution.execute_tool_block`` an
ordinary tool call goes through (``src/code_mode/bridge.py``), so a
destructive or disabled tool gets the same rejection either way.

Gated by the ``agent_code_mode`` setting (default off) -- schema and tool
are always registered, but the tool refuses to run while the setting is
off, same shape as other opt-in agent tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RunCodeTool:
    async def execute(self, content: Any, ctx: Optional[dict] = None) -> dict:
        from src.settings import get_setting

        if not get_setting("agent_code_mode", False):
            return {
                "error": "Code Mode is disabled (agent_code_mode setting is off).",
                "exit_code": 1,
            }

        raw = content if isinstance(content, str) else json.dumps(content or {})
        try:
            args = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            args = {"code": raw}
        if not isinstance(args, dict):
            args = {"code": str(args)}

        code = str(args.get("code") or "")
        if not code.strip():
            return {"error": "run_code requires non-empty `code`.", "exit_code": 1}

        ctx = ctx or {}
        from src.code_mode.runner import run_code_mode
        from src.tool_execution import get_active_workspace, get_active_workspace_roots

        return await run_code_mode(
            code,
            session_id=ctx.get("session_id"),
            owner=ctx.get("owner"),
            disabled_tools=ctx.get("disabled_tools") or set(),
            workspace=get_active_workspace(),
            workspace_roots=list(get_active_workspace_roots()),
        )
