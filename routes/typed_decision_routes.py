"""routes/typed_decision_routes.py — HTTP surface for typed decisions
(``src/typed_decision.py``).

POST /api/typed-decision
    {"context": str, "fields": [{"name", "question", "choices": [..] | "bool",
     "descriptions"?: [..] | {..}}], "purpose"?: "utility"|"task"|"research"|"default",
     "instructions"?: str, "timeout_ms"?: int, "min_confidence"?: float}
    -> {"decisions": {name: Decision}, "stats": {...}}

GET /api/typed-decision/stats
    -> process-wide counters (calls, per-method counts, fallbacks, p50/p95
       latency, the last few decisions WITHOUT their context).

Per-user like the other data routes (``require_user``); the endpoint is
resolved for the calling user. A decision is advisory — this route never
changes anything, it only answers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)

_PURPOSES = ("utility", "task", "research", "default")
MAX_FIELDS = 8
MAX_CONTEXT_CHARS = 20000


class TypedDecisionRequest(BaseModel):
    context: str = ""
    fields: List[Dict[str, Any]] = []
    purpose: Optional[str] = None
    instructions: str = ""
    timeout_ms: Optional[int] = None
    min_confidence: Optional[float] = None


def _owner(request: Request) -> Optional[str]:
    try:
        return effective_user(request) or None
    except Exception:  # noqa: BLE001
        return None


def setup_typed_decision_routes() -> APIRouter:
    router = APIRouter(prefix="/api/typed-decision", tags=["typed-decision"])

    @router.post("")
    async def post_typed_decision(request: Request, body: TypedDecisionRequest,
                                  _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import typed_decision

        context = str(body.context or "")
        if not context.strip():
            raise HTTPException(400, "'context' is required")
        if len(context) > MAX_CONTEXT_CHARS:
            raise HTTPException(413, f"'context' is longer than {MAX_CONTEXT_CHARS} characters")
        raw_fields = list(body.fields or [])
        if not raw_fields or len(raw_fields) > MAX_FIELDS:
            raise HTTPException(400, f"'fields' must list 1 to {MAX_FIELDS} questions")
        fields = []
        names = set()
        for raw in raw_fields:
            fld = typed_decision.field_from_dict(raw)
            if fld is None:
                raise HTTPException(400, "each field needs 'name', 'question' and 'choices' "
                                         "(a list of 2+ answers or \"bool\")")
            if fld.name in names:
                raise HTTPException(400, f"duplicate field name: {fld.name!r}")
            names.add(fld.name)
            fields.append(fld)
        purpose = (body.purpose or "utility").strip().lower()
        if purpose not in _PURPOSES:
            raise HTTPException(400, f"'purpose' must be one of {', '.join(_PURPOSES)}")
        timeout_s = None
        if body.timeout_ms is not None:
            timeout_s = max(50, min(10000, int(body.timeout_ms))) / 1000.0
        min_conf = None
        if body.min_confidence is not None:
            min_conf = max(0.0, min(1.0, float(body.min_confidence)))
        decisions = await typed_decision.decide(
            context, fields, owner=_owner(request), purpose=purpose,
            instructions=body.instructions or "", timeout_s=timeout_s,
            min_confidence=min_conf, caller="api",
        )
        return {"decisions": {name: d.to_dict() for name, d in decisions.items()}}

    @router.get("/stats")
    async def get_typed_decision_stats(_u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import typed_decision
        return typed_decision.stats()

    return router


__all__ = ["setup_typed_decision_routes"]
