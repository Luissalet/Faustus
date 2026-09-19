"""routes/llm_trace_routes.py — debugging aid API for src/llm_trace.py.

GET  /api/llm-traces/{session_id}            -> summary rows (list)
GET  /api/llm-traces/{session_id}/{seq}      -> one full recorded call
POST /api/llm-traces/{session_id}/{seq}/fork -> re-send that exact request to
                                                 another model/endpoint and
                                                 return both outputs side by
                                                 side

Auth follows the same seam every other per-session route uses (see
routes/agent_progress_routes.py, routes/chat_routes.py): ``require_user`` for
"is this an authenticated caller at all", then ``_verify_session_owner`` (the
one place session ownership is checked, so this file never re-implements
that comparison) for "does this caller own THIS session". A trace is exactly
as sensitive as the session it belongs to — it is that session's model
calls, verbatim — so it gets the same gate, not a stricter or looser one.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import require_user, effective_user
from routes.session_routes import _verify_session_owner
from src import llm_trace

logger = logging.getLogger(__name__)


class ForkRequest(BaseModel):
    model: Optional[str] = Field(None, max_length=300)
    endpoint_id: Optional[str] = Field(None, max_length=200)


def setup_llm_trace_routes() -> APIRouter:
    router = APIRouter(prefix="/api/llm-traces", tags=["llm-traces"])

    @router.get("/{session_id}")
    def list_traces(request: Request, session_id: str):
        require_user(request)
        _verify_session_owner(request, session_id)
        return {"session_id": session_id, "calls": llm_trace.list_calls(session_id)}

    @router.get("/{session_id}/{seq}")
    def get_trace(request: Request, session_id: str, seq: int):
        require_user(request)
        _verify_session_owner(request, session_id)
        rec = llm_trace.get_call(session_id, seq)
        if rec is None:
            raise HTTPException(404, f"No traced call {seq} for session {session_id}")
        return rec

    @router.post("/{session_id}/{seq}/fork")
    async def fork_trace(request: Request, session_id: str, seq: int, body: ForkRequest):
        require_user(request)
        _verify_session_owner(request, session_id)
        original = llm_trace.get_call(session_id, seq)
        if original is None:
            raise HTTPException(404, f"No traced call {seq} for session {session_id}")

        req = original.get("request") or {}
        if req.get("messages_omitted"):
            raise HTTPException(
                409,
                "This call's request was too large to store and cannot be forked "
                "(see request.messages_omitted for the fingerprint).",
            )
        messages = req.get("messages") or []
        def _num(value):
            # Only real numbers go back to the model; anything else (an old
            # record with a redacted placeholder, say) falls back to defaults.
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        temperature = _num(req.get("temperature"))
        max_tokens = _num(req.get("max_tokens"))
        tools = req.get("tools")

        owner = effective_user(request) or None
        fork_model = (body.model or original.get("model") or "").strip()
        if not fork_model:
            raise HTTPException(400, "No model to fork to (recorded call has no model, and none was given)")

        # Resolve the endpoint through the SAME resolution the chat path
        # uses (src/endpoint_resolver.py), never by reusing the original
        # call's (redacted) headers — a trace never holds live credentials.
        endpoint_url: Optional[str] = None
        headers: Optional[Dict[str, Any]] = None
        try:
            from src.endpoint_resolver import resolve_endpoint_by_id, resolve_endpoint
            if body.endpoint_id:
                resolved = resolve_endpoint_by_id(body.endpoint_id, fork_model, owner=owner)
                if resolved is None:
                    raise HTTPException(404, f"Endpoint {body.endpoint_id} not found")
                endpoint_url, fork_model, headers = resolved
            else:
                endpoint_url, fork_model, headers = resolve_endpoint(
                    "default",
                    fallback_url=original.get("endpoint"),
                    fallback_model=fork_model,
                    owner=owner,
                )
                if not fork_model:
                    fork_model = original.get("model")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_trace] fork endpoint resolution failed: %s", exc)
            raise HTTPException(502, f"Could not resolve an endpoint to fork to: {exc}")

        if not endpoint_url:
            raise HTTPException(400, "No endpoint available to fork to")

        from src import llm_core
        acc = llm_trace.StreamAccumulator()
        t0 = time.time()
        fork_error: Optional[str] = None
        try:
            async for chunk in llm_core.stream_llm(
                endpoint_url,
                fork_model,
                messages,
                temperature=temperature if temperature is not None else llm_core.LLMConfig.DEFAULT_TEMPERATURE,
                max_tokens=max_tokens if max_tokens is not None else llm_core.LLMConfig.DEFAULT_MAX_TOKENS,
                headers=headers,
                tools=tools,
                # Forked calls are their own debugging artifact, not part of
                # the session's own turn history, and must not recurse into
                # forking themselves — no session_id means llm_trace records
                # nothing for this call (see src/llm_trace.py).
                session_id=None,
            ):
                acc.feed(chunk)
        except HTTPException as exc:
            fork_error = str(exc.detail)
        except Exception as exc:  # noqa: BLE001
            fork_error = str(exc)
        duration_ms = (time.time() - t0) * 1000.0

        return {
            "original": {
                "seq": original.get("seq"),
                "model": original.get("model"),
                "endpoint": original.get("endpoint"),
                "response_text": original.get("response_text"),
                "thinking_text": original.get("thinking_text"),
                "tool_calls": original.get("tool_calls"),
                "finish_reason": original.get("finish_reason"),
                "usage": original.get("usage"),
                "duration_ms": original.get("duration_ms"),
                "error": original.get("error"),
            },
            "fork": {
                "model": fork_model,
                "endpoint": llm_trace._endpoint_host(endpoint_url),
                "text": acc.text,
                "thinking_text": acc.thinking,
                "tool_calls": acc.tool_calls,
                "finish_reason": acc.finish_reason,
                "usage": acc.usage,
                "duration_ms": duration_ms,
                "error": fork_error or acc.error,
            },
        }

    return router
