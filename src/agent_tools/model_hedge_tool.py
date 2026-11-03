"""agent_tools/model_hedge_tool.py - `model_hedge`: hedged first token settings and races."""
import json
import logging
from typing import Any, Dict

from src import model_hedge

logger = logging.getLogger(__name__)


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"action": raw} if raw else {}


def _render(status: Dict[str, Any]) -> str:
    lines = [
        f"Hedged first token: {'on' if status.get('enabled') else 'off'}; "
        f"hedge after {status.get('delay_s'):g} s without a first token; target {status.get('target')}.",
    ]
    if status.get("delay_overrides"):
        lines.append("Per-model delays: " + ", ".join(
            f"{m} {s:g} s" for m, s in status["delay_overrides"].items()))
    current = status.get("current")
    if current:
        hedge = current.get("hedge")
        lines.append(
            f"Default model {current.get('model')} at {current.get('endpoint_label')}: "
            + (f"hedge {hedge.get('model')} at {hedge.get('endpoint_label')} ({hedge.get('kind')})"
               if hedge else "no hedge target available"))
    stats = status.get("stats") or {}
    lines.append(f"Races recorded: {stats.get('races', 0)}, answered by the hedge: {stats.get('hedge_wins', 0)}.")
    for item in (status.get("recent") or [])[:5]:
        lines.append(
            f"- {item.get('winner')} ({item.get('outcome')}) after {item.get('after_s')} s: "
            f"{item.get('answered_by')} at {item.get('answered_by_endpoint_label')}")
    return "\n".join(lines)


class ModelHedgeTool:
    """`model_hedge` {action?, enabled?, delay_s?, target?, delay_overrides?, limit?}:
    status (default) shows the settings, the hedge the default model would get,
    the selectable targets and recent races; set changes any subset."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "status").strip().lower()
        try:
            if action in ("set", "update", "configure"):
                patch = {k: args[k] for k in ("enabled", "delay_s", "target", "delay_overrides") if k in args}
                status = model_hedge.update(patch)
            elif action in ("status", "show", "get", "races", "targets"):
                status = model_hedge.status(limit=int(args.get("limit") or 20))
            else:
                return {"error": f"model_hedge: unknown action {action!r} (status or set)", "exit_code": 1}
        except ValueError as exc:
            return {"error": f"model_hedge: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_hedge failed: %s", exc)
            return {"error": f"model_hedge: {exc}", "exit_code": 1}
        if action not in ("targets",):
            status = {**status, "targets": status.get("targets", [])[:40]}
        return {"output": _render(status), "exit_code": 0, "status": status}
