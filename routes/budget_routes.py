"""Budget accounts API — /api/runs/{run_id}/budget (A31, src/budget_account.py).

GET /api/runs/{run_id}/budget → the run's ceiling, what is currently
reserved (children in flight), what has been consumed (children finished,
retries included) and the unpriced share of that consumption.

This is a NEW route module (docs/spec/paridad/CONTRATO.md, lot T7 — file
ownership is disjoint from every other lot's routes). It is written to be
registered the same way every other router in `app.py` is
(`app.include_router(setup_budget_routes())`); wiring that one line into
`app.py` itself is out of this lot's file ownership and is left as an exact
diff in `T7_wiring.md`, per rule 6 of the lot contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from src.auth_helpers import require_user


def setup_budget_routes() -> APIRouter:
    router = APIRouter(prefix="/api/runs", tags=["budget"])

    @router.get("/{run_id}/budget")
    def get_budget(request: Request, run_id: str):
        # Best-effort auth only (matches the rest of this lot's module: the
        # ledger is keyed by run_id, not by owner — LOCALHOST_BYPASS runs
        # with no login at all, and a run_id is not guessable the way a
        # sequential integer would be). `require_user` still rejects an
        # unauthenticated caller when AUTH_ENABLED=true.
        require_user(request)
        from src import budget_account
        return budget_account.snapshot(run_id)

    return router
