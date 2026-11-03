"""agent_tools/derivation_tools.py — recipe_learn, derivation_graph and
decision_map: thin executors over `src.derivation_ops`."""
import asyncio
import json
from typing import Any, Callable, Dict

from src import derivation_ops


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _run(fn: Callable[..., Dict[str, Any]], content: str, ctx: dict, name: str) -> dict:
    args = _args(content)
    if not args:
        return {"error": f"{name}: the arguments must be a JSON object with an action", "exit_code": 1}
    owner = str((ctx or {}).get("owner") or "")
    out = await asyncio.to_thread(fn, owner, args)
    out.setdefault("exit_code", 0)
    return out


class RecipeLearnTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _run(derivation_ops.run_recipe, content, ctx, "recipe_learn")


class DerivationGraphTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _run(derivation_ops.run_graph, content, ctx, "derivation_graph")


class DecisionMapTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _run(derivation_ops.run_map, content, ctx, "decision_map")
