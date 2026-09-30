"""Capability probes for OpenAI-compatible endpoints — /api/model-probes/*.

  GET  /api/model-probes/openai?endpoint_id=&model=   declared vs probed, from the store
  POST /api/model-probes/openai                        {endpoint_id, model, probes?} run and file
  POST /api/model-probes/openai/url                    {base_url, model, api_key?, probes?} run, nothing filed

The read never touches the network. The two POSTs send requests with the
endpoint's own credentials (or the key given in the body), so they are admin AND
same-origin, like every other route that makes the server act on a model
server. ``src/openai_probes.py`` holds the semantics: supported / unsupported /
unknown, filed under the endpoint's connection revision as it was before the
first request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.middleware import require_admin
from routes.workspace_routes import _reject_cross_origin
from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def setup_model_probe_routes() -> APIRouter:
    router = APIRouter(prefix="/api/model-probes", tags=["model-probes"])

    def _owner(request: Request) -> Optional[str]:
        try:
            return effective_user(request) or None
        except Exception:  # noqa: BLE001
            return None

    async def _body(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - an empty or non-JSON body is a 400
            data = {}
        return data if isinstance(data, dict) else {}

    def _probes(body: Dict[str, Any]):
        value = body.get("probes")
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise HTTPException(status_code=400, detail="probes must be a list of names")
        return value

    async def _run(fn, *args, **kwargs) -> Dict[str, Any]:
        from src import openai_probes
        from src.endpoint_resolver import EndpointConfigurationChanged
        try:
            return {"status": "success", **await asyncio.to_thread(fn, *args, **kwargs)}
        except openai_probes.ProbeRefused as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        except EndpointConfigurationChanged:
            raise HTTPException(status_code=409, detail="the endpoint configuration changed while probing")

    @router.get("/openai")
    async def read_probes(request: Request, endpoint_id: str = Query(default=""),
                          model: str = Query(default="")) -> Dict[str, Any]:
        require_user(request)
        from src import openai_probes
        return await _run(openai_probes.read_endpoint, endpoint_id, model, owner=_owner(request))

    @router.post("/openai")
    async def run_probes(request: Request) -> Dict[str, Any]:
        require_admin(request)
        _reject_cross_origin(request)
        body = await _body(request)
        from src import openai_probes
        return await _run(openai_probes.probe_endpoint, str(body.get("endpoint_id") or ""),
                          str(body.get("model") or ""), owner=_owner(request), include=_probes(body))

    @router.post("/openai/url")
    async def run_probes_for_url(request: Request) -> Dict[str, Any]:
        require_admin(request)
        _reject_cross_origin(request)
        body = await _body(request)
        from src import openai_probes
        return await _run(openai_probes.probe_url, str(body.get("base_url") or ""),
                          str(body.get("model") or ""), api_key=str(body.get("api_key") or ""),
                          include=_probes(body))

    return router
