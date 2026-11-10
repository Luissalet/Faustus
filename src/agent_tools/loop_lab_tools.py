"""agent_tools/loop_lab_tools.py -- `loop_card_plan`, `loop_arms`, `loop_improve`, `prompt_mirror`.

* loop_card_plan: pre-register a Loop Card's keep bar, minimum pairs, guard command, stop rule
  and timebox (`src.loop_card.update_plan`).
* loop_arms: measure variants against the card's control in paired, interleaved rounds and
  keep only what clears the bar (`src.loop_arms.run_arms`). Runs the measuring command.
* loop_improve: the improvement loop -- hypotheses, one fresh worker per proposal, isolated
  alternatives, guard, paired measurement, apply the winner (`src.loop_arms`). Background job.
* prompt_mirror: a scorecard of how the owner directs Faustus, from their own chats
  (`src.prompt_mirror`).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from src import loop_arms, loop_card, prompt_mirror


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


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


def _err(tool: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"error": f"{tool}: {data['error']}", "error_class": data.get("error_class"), "exit_code": 1}


class LoopCardPlanTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        data = await asyncio.to_thread(loop_card.run, _owner(ctx), {**args, "action": "plan"})
        if data.get("error"):
            return _err("loop_card_plan", data)
        return {"output": loop_card.render(data), "exit_code": 0, "result": loop_card._row(data)}


class LoopArmsTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        if not args:
            return {"error": "loop_arms: the arguments must be a JSON object", "exit_code": 1}
        data = await asyncio.to_thread(loop_arms.run_arms_action, _owner(ctx), args)
        if data.get("error"):
            return _err("loop_arms", data)
        slim = {k: v for k, v in data.items() if k != "samples"}
        return {"output": loop_arms.render_arms(data), "exit_code": 0, "result": slim}


class LoopImproveTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        if not args.get("project") and (ctx or {}).get("project_id"):
            args["project"] = str(ctx["project_id"])
        data = await loop_arms.run_improve_action(_owner(ctx), args)
        if data.get("error"):
            return _err("loop_improve", data)
        return {"output": loop_arms.render_job(data), "exit_code": 0,
                "result": {k: v for k, v in data.items() if k not in ("log",)}}


class PromptMirrorTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        if str(args.get("action") or "") == "history":
            data = await asyncio.to_thread(prompt_mirror.history, _owner(ctx))
        else:
            data = await asyncio.to_thread(prompt_mirror.run, _owner(ctx), args)
        if data.get("error"):
            return _err("prompt_mirror", data)
        return {"output": prompt_mirror.render(data), "exit_code": 0}


HANDLERS = {"loop_card_plan": LoopCardPlanTool, "loop_arms": LoopArmsTool, "loop_improve": LoopImproveTool,
            "prompt_mirror": PromptMirrorTool}
