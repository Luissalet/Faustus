"""routes/run_report_routes.py -- what a run did, what it cost, and what it left open.

Read-mostly views over four durable records, all owner-scoped:

GET  /api/effects                         effects that leave the machine (mail, messages, webhooks,
                                          calendar and HTTP writes, connector writes): state, certainty,
                                          attempt id, whether the outcome is still unknown
GET  /api/effects/{id}                    one effect with its transition history
POST /api/effects/{id}/reconcile          ask the destination (never sends again)
POST /api/effects/{id}/resolve            a person records what they found at the destination
GET  /api/runs/{session_id}/ledger        the causal ledger of a session's runs, or one run replayed
GET  /api/runs/{session_id}/turn-cost     where one turn's time and tokens went, by cause
GET  /api/agent/orphans                   workers whose parent run is gone
POST /api/agent/orphans/{key}/kill        stop one of them

Nothing here re-executes anything. See src/effect_outbox.py, src/exec_ledger.py,
src/turn_cost.py and src/orphan_workers.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user, require_user
from src.run_report_render import effects_text, ledger_text, orphans_text


def _owner(request: Request) -> str:
    return str(effective_user(request) or "")


def _owns(request: Request):
    def check(session_id: str) -> bool:
        if not session_id:
            return False
        try:
            _verify_session_owner(request, session_id)
            return True
        except HTTPException:
            return False
    return check


def setup_run_report_routes() -> APIRouter:
    router = APIRouter(tags=["run-report"])

    # ------------------------------------------------------------ effects
    @router.get("/api/effects")
    async def list_effects(request: Request, state: Optional[str] = None, session_id: Optional[str] = None,
                           kind: Optional[str] = None, unresolved: bool = False, limit: int = 100,
                           _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import effect_outbox
        owner = _owner(request)
        if unresolved:
            rows = effect_outbox.needs_reconciliation(owner=owner, limit=max(1, min(limit, 500)))
        else:
            rows = effect_outbox.list_effects(owner=owner, state=state, session_id=session_id, kind=kind,
                                              limit=limit)
        summary = effect_outbox.summary(owner=owner)
        return {"effects": rows, "summary": summary, "text": effects_text(rows, summary)}

    @router.get("/api/effects/{effect_id}")
    async def get_effect(request: Request, effect_id: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import effect_outbox
        row = effect_outbox.get(effect_id, owner=_owner(request))
        if row is None:
            raise HTTPException(404, "Effect not found")
        return {"effect": row, "events": effect_outbox.events(effect_id)}

    @router.post("/api/effects/{effect_id}/reconcile")
    async def reconcile_effect(request: Request, effect_id: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import effect_outbox
        out = effect_outbox.reconcile(effect_id, owner=_owner(request))
        if out.get("reason") == "not_found":
            raise HTTPException(404, "Effect not found")
        return out

    @router.post("/api/effects/{effect_id}/resolve")
    async def resolve_effect(request: Request, effect_id: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import effect_outbox
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("landed"), bool):
            raise HTTPException(400, "Body must be {\"landed\": true|false, \"note\": \"...\"}")
        out = effect_outbox.resolve_manually(
            effect_id, landed=body["landed"], note=str(body.get("note") or ""), actor=_owner(request),
            owner=_owner(request))
        if out.get("reason") == "not_found":
            raise HTTPException(404, "Effect not found")
        if not out.get("ok"):
            raise HTTPException(409, out.get("reason") or "Not reconcilable")
        return out

    # ------------------------------------------------------------- ledger
    @router.get("/api/runs/{session_id}/ledger")
    async def run_ledger(request: Request, session_id: str, run_id: Optional[str] = None, events: bool = False,
                         limit: int = 30, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import exec_ledger
        _verify_session_owner(request, session_id)
        if not run_id:
            runs = exec_ledger.list_runs(session_id=session_id, limit=limit)
            return {"session_id": session_id, "runs": runs, "summary": exec_ledger.summary(),
                    "text": "\n".join(f"- {r['run_id'][:12]} {r['state']}: {r['calls']} call(s), "
                                      f"{r['pending_approvals']} awaiting approval, {r['unresolved']} unresolved"
                                      for r in runs) or "No recorded runs."}
        state = exec_ledger.replay(run_id)
        if state["session_id"] != session_id:
            raise HTTPException(404, "Run not found in this session")
        out: Dict[str, Any] = {"replay": state, "text": ledger_text(state)}
        if events:
            out["events"] = exec_ledger.events(run_id, limit=2000)
        return out

    @router.get("/api/runs/{session_id}/turn-cost")
    async def turn_cost(request: Request, session_id: str, run_id: Optional[str] = None, turn: int = 0,
                        _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import turn_cost as _turn_cost
        _verify_session_owner(request, session_id)
        view = _turn_cost.build(session_id, run_id, turn=turn)
        if run_id and view.get("found") and view.get("session_id") != session_id:
            raise HTTPException(404, "Run not found in this session")
        return {**view, "text": _turn_cost.render_text(view)}

    # ------------------------------------------------------------ orphans
    @router.get("/api/agent/orphans")
    async def list_orphans(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import orphan_workers
        found = orphan_workers.scan(owns=_owns(request))
        return {"orphans": found, "count": len(found), "grace_s": orphan_workers.GRACE_S,
                "text": orphans_text(found)}

    @router.post("/api/agent/orphans/{worker_key}/kill")
    async def kill_orphan(request: Request, worker_key: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import orphan_workers
        out = orphan_workers.kill(worker_key, owns=_owns(request))
        if not out.get("stopped") and out.get("reason", "").startswith("not an orphan"):
            raise HTTPException(404, out["reason"])
        return out

    return router


__all__ = ["setup_run_report_routes"]
