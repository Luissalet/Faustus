"""agent_tools/run_report_tool.py — `run_report`: what a run did, cost and left open.

One read tool over the durable records of the outbox, the execution ledger,
the steering journal and the worker registry:

* ``effects``  actions that left the machine (mail, messages, webhooks, calendar
               and HTTP writes, connector writes): state, certainty, whether the
               outcome is still unknown. With ``unresolved`` it first asks the
               destination about each unresolved one (never sends again).
* ``ledger``   one run replayed from the execution ledger: calls, attempts,
               approvals, what resumed.
* ``cost``     where one turn's time and tokens went, by cause. Unknown stays
               unknown.
* ``steering`` messages sent to a live run and what became of each.
* ``orphans``  workers whose parent run is gone.
"""
import json
import logging
from typing import Any, Dict

from src.run_report_render import effects_text, ledger_text, orphans_text, steering_text

logger = logging.getLogger(__name__)

VIEWS = ("effects", "ledger", "cost", "steering", "orphans")


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"view": raw} if raw in VIEWS else {}


def _owns(owner: str):
    def check(session_id: str) -> bool:
        if not session_id:
            return False
        try:
            from src.ai_interaction import get_session_manager
            sm = get_session_manager()
            sess = sm.get_session(session_id) if sm else None
        except Exception:  # noqa: BLE001
            return False
        return sess is not None and (not owner or getattr(sess, "owner", None) in (None, "", owner))
    return check


class RunReportTool:
    """`run_report` {view, session_id?, run_id?, turn?, unresolved?, state?, limit?}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        view = str(args.get("view") or "").strip().lower()
        if view not in VIEWS:
            return {"error": f"run_report: view must be one of {', '.join(VIEWS)}", "exit_code": 1}
        owner = str(ctx.get("owner") or "")
        sid = str(args.get("session_id") or ctx.get("session_id") or "").strip()
        if sid and not _owns(owner)(sid):
            return {"error": f"run_report: chat {sid} not found", "exit_code": 1}
        try:
            if view == "effects":
                from src import effect_outbox
                if args.get("unresolved") or args.get("reconcile"):
                    try:
                        effect_outbox.reconcile_pending(owner=owner, limit=20)
                    except Exception:  # noqa: BLE001 - a destination that cannot be reached leaves the row unknown
                        logger.debug("run_report: reconcile_pending failed", exc_info=True)
                    rows = effect_outbox.needs_reconciliation(owner=owner, limit=int(args.get("limit") or 50))
                else:
                    rows = effect_outbox.list_effects(owner=owner, state=args.get("state"), session_id=sid or None,
                                                      limit=int(args.get("limit") or 30))
                summary = effect_outbox.summary(owner=owner)
                return {"output": effects_text(rows, summary), "exit_code": 0, "effects": rows, "summary": summary}
            if view == "ledger":
                from src import exec_ledger, turn_cost
                rid = turn_cost.resolve_run(sid, args.get("run_id"), int(args.get("turn") or 0)) if sid else args.get("run_id")
                if not rid:
                    return {"output": "No recorded run.", "exit_code": 0}
                state = exec_ledger.replay(str(rid), owner=owner or None)
                if sid and state["session_id"] not in ("", sid):
                    return {"error": "run_report: run not found in this chat", "exit_code": 1}
                return {"output": ledger_text(state), "exit_code": 0, "replay": state}
            if view == "cost":
                from src import turn_cost
                if not sid:
                    return {"error": "run_report: no chat (pass session_id)", "exit_code": 1}
                data = turn_cost.build(sid, args.get("run_id"), turn=int(args.get("turn") or 0))
                return {"output": turn_cost.render_text(data), "exit_code": 0, "cost": data}
            if view == "steering":
                from src import steering_journal
                if not sid:
                    return {"error": "run_report: no chat (pass session_id)", "exit_code": 1}
                recs = steering_journal.receipts(session_id=sid, owner=owner or None, limit=int(args.get("limit") or 30))
                return {"output": steering_text(recs), "exit_code": 0, "receipts": recs}
            from src import orphan_workers
            found = orphan_workers.scan(owns=_owns(owner))
            return {"output": orphans_text(found), "exit_code": 0, "orphans": found}
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_report failed", exc_info=True)
            return {"error": f"run_report: {type(exc).__name__}: {exc}", "exit_code": 1}
