"""routes/inference_routes.py — INF-02 §06/§07: read-only capability
assessment and per-session launch receipts, registered in `app.py` next to
`board_routes` (a new router rather than folding into the very large
`cookbook_routes.py`, per CONTRATO_INF02 Lote A / A5).

Three endpoints, none of which load a model, install anything, or restart a
process — "verifying is reading" (§06):

  POST /api/model/serve/assess              — pure: assess a plan's options
                                               against the manifest, no side
                                               effects, usable before a
                                               launch even exists.
  GET  /api/model/serve/{id}/receipt        — read the receipt a prior
                                               `POST /api/model/serve` filed.
  POST /api/model/serve/{id}/verify         — passive probes only (5 s
                                               timeout each); see
                                               `src.launch_receipts.verify`.

Errors are flat `{"error", "error_class"}` bodies (CONTRATO_INF02's own
convention, matching `routes/condense_routes.py`'s `_error()`), never a
bare `HTTPException(status, "message")` — FastAPI would nest that under
`"detail"` instead of putting `error_class` at the top level.

Gating: `require_admin`, same as every other Cookbook serve route — this
reads/assesses process-launch state, not the caller's own data.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src import inference_capabilities, launch_receipts
from src.contracts.base import ContractError
from src.contracts.inference import IMPLEMENTATIONS

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error": message, "error_class": error_class}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class AssessRequest(BaseModel):
    implementation: str
    model: Any = None
    options: dict = {}
    arch: dict | None = None
    engine_version: str | None = None
    gpus: list | None = None


class VerifyRequest(BaseModel):
    base_url: str | None = None
    authorized_probe: bool = False


# Mirrors `routes/cookbook_routes.py`'s own `_ollama_bind_from_cmd`-based port
# lookup for other implementations — kept local rather than imported from
# `routes.cookbook_helpers` so this router does not reach back into a very
# large sibling module for one regex; `_ollama_bind_from_cmd` itself IS
# reused below because Ollama's host/port parsing (bracketed IPv6, env var
# assignment shape) is exactly INF-01's own logic and must not fork.
_PORT_FLAG_RE = re.compile(r"(?:--port|-p)\s+(\d{2,5})\b")


def _infer_base_url(receipt) -> Optional[str]:
    """Best-effort `base_url` for `verify()`, from the receipt's own
    command/engine fields — never from the model's name or any other
    untrusted source. `None` means the caller must supply `base_url`
    explicitly; the route turns that into `400 serve.base_url_required`,
    never a guess."""
    cmd = receipt.final_cmd or receipt.requested_cmd or ""
    implementation = receipt.engine.implementation
    if implementation == "ollama":
        from routes.cookbook_helpers import _ollama_bind_from_cmd
        host, port = _ollama_bind_from_cmd(cmd)
        host = host.strip("[]") or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    match = _PORT_FLAG_RE.search(cmd)
    port = match.group(1) if match else (str(receipt.engine.port) if receipt.engine.port else None)
    if not port:
        return None
    host = receipt.engine.host or "127.0.0.1"
    if host in ("0.0.0.0", "", "local"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def setup_inference_routes() -> APIRouter:
    router = APIRouter(tags=["inference"])

    @router.post("/api/model/serve/assess")
    async def assess(request: Request, req: AssessRequest):
        """Pure: no process is touched. The UI calls this on every option
        change (debounced) to show Capabilities before Launch is even
        pressed; `POST /api/model/serve` re-runs the identical check server
        -side before it launches anything, so this endpoint's answer is
        never trusted on its own to authorize a launch."""
        require_admin(request)
        if req.implementation not in IMPLEMENTATIONS:
            return _error(
                400,
                f"implementation must be one of {list(IMPLEMENTATIONS)}",
                "serve.invalid_plan",
            )
        try:
            assessments = inference_capabilities.assess_options(
                req.implementation,
                req.options or {},
                arch=req.arch,
                engine_version=req.engine_version,
                gpus=req.gpus,
            )
        except ContractError as e:
            return _error(400, str(e), "serve.invalid_plan")
        blockers = inference_capabilities.hard_blockers(assessments)
        return {
            "assessments": [a.to_dict() for a in assessments],
            "blockers": [b.to_dict() for b in blockers],
        }

    @router.get("/api/model/serve/{session_id}/receipt")
    async def get_receipt(request: Request, session_id: str):
        require_admin(request)
        try:
            receipt = launch_receipts.get(session_id)
        except launch_receipts.LaunchReceiptError as e:
            return _error(404, str(e), "serve.receipt_not_found")
        if receipt is None:
            return _error(404, f"no launch receipt for session {session_id!r}", "serve.receipt_not_found")
        return {"receipt": receipt.to_dict()}

    @router.post("/api/model/serve/{session_id}/verify")
    async def verify_receipt(request: Request, session_id: str, req: VerifyRequest):
        """Passive verification only — see `src.launch_receipts.verify`'s
        module docstring. `authorized_probe` defaults to `False` on every
        single call; nothing here remembers a prior authorization."""
        require_admin(request)
        try:
            receipt = launch_receipts.get(session_id)
        except launch_receipts.LaunchReceiptError as e:
            return _error(404, str(e), "serve.receipt_not_found")
        if receipt is None:
            return _error(404, f"no launch receipt for session {session_id!r}", "serve.receipt_not_found")

        base_url = (req.base_url or "").strip() or _infer_base_url(receipt)
        if not base_url:
            return _error(
                400,
                "could not determine base_url for this session; pass one explicitly",
                "serve.base_url_required",
            )
        try:
            verified = await launch_receipts.verify(
                session_id, base_url=base_url, authorized_probe=req.authorized_probe,
            )
        except launch_receipts.LaunchReceiptError as e:
            return _error(404, str(e), "serve.receipt_not_found")
        return {"receipt": verified.to_dict()}

    return router
