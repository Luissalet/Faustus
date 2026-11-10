"""agent_tools/brain_bases_tools.py -- `brain_base`: Obsidian Bases (.base files) over the
Cerebro's notes (`src.brain.bases`): list, read, evaluate a view, validate, save, delete."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict


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


class BrainBaseTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        from src.brain import bases
        args = _args(content)
        data = await asyncio.to_thread(bases.run, str((ctx or {}).get("owner") or ""), args)
        if data.get("error"):
            return {"error": f"brain_base: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}
        return {"output": bases.render_text(data), "exit_code": 0}


HANDLERS = {"brain_base": BrainBaseTool}
