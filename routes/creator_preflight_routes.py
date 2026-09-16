"""Creator preflight routes (WP09): plan, then approve, over HTTP.

Two routes, both gated by the ``creator_enabled`` setting (default False,
``src/settings.py``), checked BEFORE any store access — same discipline as
``routes/creator_routes.py`` (CONTRATO.md rule 5): with the flag off, both
routes answer 404 and nothing is read or written.

* ``POST /api/creator/preflight`` — runs ``src.creator.preflight.run_preflight``
  (pure, no side effects) and returns its report.
* ``POST /api/creator/preflight/{digest}/approve`` — the ONE place an
  approval is actually created for a Creator operation. It recomputes the
  same pure preflight from the request body, refuses (409) if the digest it
  gets does not match the path (the plan changed since the caller last saw
  it — material-change invalidation, before an approval even exists), and
  only then opens and immediately grants a real ``approval_store`` card for
  that exact plan. Gated by ``require_human``: the model's own loopback
  token cannot reach it, the same rule ``routes/approvals_routes.py`` uses
  for grant/deny — a Creator operation approving its own budget by calling
  this endpoint itself would defeat the whole point of asking.

Owner is always resolved from the authenticated session
(CONTRATO.md rule 3), never from the request body.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    owner = effective_storage_owner(user, auth_is_disabled=mw.auth_disabled())
    return owner or ""


def _current_user(request: Request) -> str:
    return str(getattr(request.state, "current_user", "") or "").strip()


def _creator_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("creator_enabled", False))


def _require_flag() -> None:
    if not _creator_enabled():
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _parse_preflight_body(payload: Dict[str, Any], *, owner: str) -> Dict[str, Any]:
    project_id = str(payload.get("project_id") or "")
    operation = str(payload.get("operation") or "")
    engine = str(payload.get("engine") or "")
    deployment_id = str(payload.get("deployment_id") or "")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        raise HTTPException(400, "inputs must be a list")

    if not project_id:
        raise HTTPException(400, "project_id is required")
    if not operation:
        raise HTTPException(400, "operation is required")
    if not engine:
        raise HTTPException(400, "engine is required")

    from services.projects import get_store as get_project_store
    if get_project_store().get(project_id, owner) is None:
        raise HTTPException(404, "project not found")

    return {
        "project_id": project_id, "operation": operation, "engine": engine,
        "deployment_id": deployment_id, "params": dict(params or {}),
        "inputs": [str(i) for i in inputs],
    }


def _run_preflight(owner: str, body: Dict[str, Any]):
    from src.creator import profile as profile_mod
    from src.creator.preflight import run_preflight
    from services.projects import get_store as get_project_store

    profile = profile_mod.get_profile(get_project_store(), owner, body["project_id"]) or {}
    return run_preflight(
        owner=owner, project_id=body["project_id"], operation=body["operation"],
        engine=body["engine"], deployment_id=body["deployment_id"],
        params=body["params"], inputs=body["inputs"], profile=profile,
    )


def setup_creator_preflight_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.post("/preflight")
    async def post_preflight(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        body = _parse_preflight_body(payload, owner=owner)
        report = _run_preflight(owner, body)
        return report.to_dict()

    @router.post("/preflight/{digest}/approve")
    async def post_preflight_approve(request: Request, digest: str):
        # A person, not a tool call — see module docstring. Checked before
        # the flag so a disabled Creator answers 404 either way, but an
        # in-process agent token never gets close enough to find out which.
        from core.middleware import require_human
        require_human(request)
        owner = _owner(request)
        _require_flag()

        payload = await _json_object(request)
        body = _parse_preflight_body(payload, owner=owner)
        report = _run_preflight(owner, body)

        if not report.requires_approval or not report.approval_digest:
            raise HTTPException(400, "this operation does not require approval right now")
        if report.approval_digest != digest:
            raise HTTPException(
                409,
                {
                    "reason": "digest_mismatch",
                    "detail": "the plan behind this digest has changed since preflight; "
                              "re-run preflight and approve the new digest",
                    "current_digest": report.approval_digest,
                },
            )

        from src import approval_store
        from src.contracts import ApprovalPlan

        plan = ApprovalPlan.parse(report.approval_plan)
        approval = approval_store.request(plan, owner=owner, project_id=body["project_id"])
        decided_by = _current_user(request) or "the signed-in user"
        result = approval_store.decide(approval.id, granted=True, by=decided_by,
                                        reason=f"creator preflight {digest[:16]}")
        if not result.get("ok"):
            raise HTTPException(409, result)
        return {"approval": result["approval"], "digest": digest}

    return router
