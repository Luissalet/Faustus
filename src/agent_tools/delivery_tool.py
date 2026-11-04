"""agent_tools/delivery_tool.py -- the `delivery_package` (Hangar E33) and `scope_compare` (E30) executors.

Thin dispatcher over `src.delivery_package.run`: build a reproducible delivery
package whose editables and exports were reopened from the source and from the
zip, list/show earlier packages, or reverify a stored zip.
"""
import asyncio
import json
from typing import Any, Dict

from src import delivery_package


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class DeliveryPackageTool:
    """`delivery_package` {action, name, items, instructions, limitations, software_root, ...}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        if not args:
            return {"error": "delivery_package: the arguments must be a JSON object", "exit_code": 1}
        owner = str((ctx or {}).get("owner") or "")
        data = await asyncio.to_thread(delivery_package.run, owner, args)
        if data.get("error"):
            return {"error": f"delivery_package: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}
        return {"output": delivery_package.render(data), "exit_code": 0, "result": data}


class ScopeCompareTool:
    """`scope_compare` {agreement, new_request, initial?, graph?, effort_hours?, mode?, lang?} (Hangar E30)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import scope_compare
        args = _args(content)
        if not args:
            return {"error": "scope_compare: the arguments must be a JSON object", "exit_code": 1}
        owner = str((ctx or {}).get("owner") or "")
        data = await asyncio.to_thread(scope_compare.run, owner, args)
        if data.get("error"):
            return {"error": f"scope_compare: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}
        return {"output": scope_compare.render(data), "exit_code": 0, "result": data}
