"""agent_tools/uncertainty_tool.py -- the `uncertainty_next_observation` executor.

Thin dispatcher over `src.uncertainty_case` / `src.uncertainty_sensor`: decide
which observation to make next among competing hypotheses, keeping the case
between calls when a `case_id` is given.
"""
import json
import logging
from typing import Any, Dict

from src import uncertainty_case, uncertainty_sensor

logger = logging.getLogger(__name__)


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
    return {"action": "get", "case_id": raw} if raw else {}


class UncertaintyNextObservationTool:
    """`uncertainty_next_observation` {hypotheses, candidates, observed?, case_id?, ...}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        owner = str((ctx or {}).get("owner") or "")
        try:
            data = uncertainty_case.advance(args, owner)
        except ValueError as exc:
            return {"error": f"uncertainty_next_observation: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
            logger.warning("uncertainty_next_observation failed: %s", exc)
            return {"error": f"uncertainty_next_observation: {exc}", "exit_code": 1}
        if "hypotheses" in data:
            text = uncertainty_sensor.render(data)
            if data.get("case_id"):
                text += f"\nCase: {data['case_id']}"
        else:
            text = json.dumps(data, ensure_ascii=False)
        return {"output": text, "exit_code": 0, "result": data}
