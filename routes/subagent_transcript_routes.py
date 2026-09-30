"""routes/subagent_transcript_routes.py — read-only transcript of one sub-agent.

GET /api/chat/subagent/transcript/{child_session_id}?offset=&limit=

A worker of delegate_agents is a real chat session, so its messages, the tool
calls and results stored on them, and (when call tracing kept them) its model
calls are already in the database. This route returns them in one bounded,
display-ready payload for the Agents panel. Ownership is the same gate the
worker's own stop/steer routes and the model-call trace routes use: the caller
must own the session (`_verify_session_owner`), so another user's sub-agent
answers 404 exactly like a missing one. Nothing is written.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.database import SessionLocal, ChatMessage as DbChatMessage, Session as DbSession
from routes.session_routes import _verify_session_owner
from src import subagent_transcript
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _content_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
    return "" if raw is None else str(raw)


def setup_subagent_transcript_routes() -> APIRouter:
    router = APIRouter(tags=["subagent-transcript"])

    @router.get("/api/chat/subagent/transcript/{child_session_id}")
    def subagent_transcript_route(request: Request, child_session_id: str,
                                  offset: Optional[int] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        require_user(request)
        _verify_session_owner(request, child_session_id)
        page_limit = max(1, min(int(limit) if limit is not None else DEFAULT_LIMIT, MAX_LIMIT))
        db = SessionLocal()
        try:
            session_row = db.query(DbSession).filter(DbSession.id == child_session_id).first()
            if session_row is None:
                raise HTTPException(404, f"Session {child_session_id} not found")
            total = db.query(DbChatMessage).filter(DbChatMessage.session_id == child_session_id).count()
            start = int(offset) if offset is not None else 0
            start = max(0, min(start, total))
            rows = (db.query(DbChatMessage).filter(DbChatMessage.session_id == child_session_id)
                    .order_by(DbChatMessage.timestamp).offset(start).limit(page_limit).all())
            name, model = session_row.name or "", session_row.model or ""
            items: List[Dict[str, Any]] = []
            for m in rows:
                meta: Dict[str, Any] = {}
                if m.meta_data:
                    try:
                        parsed = json.loads(m.meta_data)
                        meta = parsed if isinstance(parsed, dict) else {}
                    except (json.JSONDecodeError, ValueError):
                        meta = {}
                if m.timestamp and "timestamp" not in meta:
                    meta["timestamp"] = m.timestamp.isoformat() + "Z"
                items.append({"role": m.role, "content": _content_text(m.content), "metadata": meta})
        finally:
            db.close()
        calls: Optional[List[Dict[str, Any]]] = None
        try:
            from src import llm_trace
            calls = llm_trace.list_calls(child_session_id)
        except Exception as exc:  # noqa: BLE001 - tracing is optional
            logger.debug("subagent transcript: no traced calls for %s: %s", child_session_id, exc)
        return subagent_transcript.build(child_session_id, name, model, items,
                                         offset=start, total=total, calls=calls)

    return router
