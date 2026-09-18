"""Model identity vs. deployment — MOD-01/MOD-02/MOD-19 (WP06,
`src/model_identity.py`).

  GET  /api/models/identity?endpoint=&model=   ModelSpec + DeploymentManifest
                                                + evidence for `model` on
                                                `endpoint`, resolved fresh off
                                                /api/tags + /api/show. Never
                                                loads the model; persists the
                                                resolved spec/deployment rows
                                                so GET /deployments can list
                                                them afterwards.
  GET  /api/models/deployments                 Every deployment this install
                                                has ever resolved or recorded
                                                evidence for.
  POST /api/models/deployments/{id}/evidence   Add one CapabilityEvidence row
                                                by hand (a manual note, or a
                                                real measurement an operator
                                                ran outside calibration).

This is a NEW route module (CONTRATO's file-ownership table, WP06). Like
`routes/budget_routes.py` before it, wiring the one
`app.include_router(setup_model_identity_routes())` line into `app.py` is
out of this lot's file ownership; the exact diff is in `WP06_wiring.md`.

Gated on the `creator_enabled` setting (CONTRATO rule 5, default False): with
the flag off every route here answers 404 and nothing is read or written —
checked before any store access, on every route, not just the mutating one.

Reads are for any signed-in user (`require_user`); adding evidence by hand is
admin-only (`require_admin`), the same split `routes/local_models_routes.py`
already uses between reading a manifest and running/mutating one.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import require_user
from src import model_identity as mi
from src import settings as odysseus_settings

logger = logging.getLogger(__name__)


def _creator_enabled_or_404() -> None:
    if not odysseus_settings.get_setting("creator_enabled", False):
        raise HTTPException(404, "Creator is not enabled")


def _ollama_root(base_url: str) -> str:
    """`http://host:11434/v1` -> `http://host:11434` (the bare /api root).
    Mirrors `routes/local_models_routes.py::ollama_root` (a pure string
    helper; reimplemented here rather than imported so this module has no
    import-time dependency on that file's router-construction side effects)."""
    base = str(base_url or "").strip().rstrip("/")
    for suffix in ("/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


def _resolve_endpoint(endpoint: str) -> Dict[str, str]:
    """`endpoint` is either a configured `ModelEndpoint.id` or a raw base
    URL (``http://host:11434`` or with a trailing ``/v1``/``/api``) — the
    same two shapes `GET /api/models/identity`'s only caller (an operator or
    the Studio Model Explorer) is expected to have on hand. Never touches the
    network; only the endpoints table (read-only) and string parsing."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise HTTPException(400, "endpoint is required")
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.hostname:
        return {"id": endpoint, "root": _ollama_root(endpoint)}
    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint).first()
    finally:
        db.close()
    if row is None:
        raise HTTPException(404, f"Unknown endpoint: {endpoint}")
    root = _ollama_root(str(getattr(row, "base_url", "") or ""))
    if not root:
        raise HTTPException(400, f"Endpoint {endpoint} has no usable base_url")
    return {"id": endpoint, "root": root}


def setup_model_identity_routes() -> APIRouter:
    router = APIRouter(prefix="/api/models", tags=["model-identity"])

    @router.get("/identity")
    async def get_identity(
        request: Request,
        endpoint: str = Query(..., description="Configured endpoint id, or a raw Ollama base URL"),
        model: str = Query(..., description="Model name/tag as Ollama names it"),
    ) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        ep = _resolve_endpoint(endpoint)
        model = str(model or "").strip()
        if not model:
            raise HTTPException(400, "model is required")
        try:
            resolution = await _to_thread_resolve(ep["root"], model, ep["id"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        store = mi.default_store()
        store.upsert_model_spec(resolution.model_spec)
        store.upsert_deployment(resolution.deployment)
        evidence = store.list_evidence(resolution.deployment.deployment_id)
        return {
            "model_spec": resolution.model_spec.to_dict(),
            "deployment": resolution.deployment.to_dict(),
            "evidence": evidence,
        }

    @router.get("/deployments")
    async def list_deployments(request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        store = mi.default_store()
        deployments = store.list_deployments()
        for dep in deployments:
            dep["evidence_count"] = len(store.list_evidence(dep["deployment_id"]))
        return {"deployments": deployments}

    @router.post("/deployments/{deployment_id}/evidence")
    async def add_evidence(deployment_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        _creator_enabled_or_404()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        capability = str(body.get("capability") or "").strip()
        level = str(body.get("level") or "").strip().lower()
        source = str(body.get("source") or "manual").strip() or "manual"
        method = str(body.get("method") or "").strip()
        observed_at = body.get("observed_at")
        conditions = body.get("conditions") if isinstance(body.get("conditions"), dict) else {}
        if not capability:
            raise HTTPException(400, "capability is required")
        if level not in mi.LEVELS:
            raise HTTPException(400, f"level must be one of {list(mi.LEVELS)}")
        store = mi.default_store()
        evidence = store.add_evidence(
            deployment_id=deployment_id,
            capability=capability,
            level=level,
            source=source,
            observed_at=str(observed_at) if observed_at else None,
            conditions=conditions,
            method=method,
        )
        return {"evidence": evidence.to_dict()}

    return router


async def _to_thread_resolve(root: str, model: str, endpoint_id: str) -> mi.Resolution:
    import asyncio
    return await asyncio.to_thread(mi.resolve_deployment, root, model, endpoint_id=endpoint_id)
