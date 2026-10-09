"""routes/voice_timeline_routes.py - latency timeline of voice turns.

    POST /api/voice/timeline             {session_id, turn_id, marks...}   record (or complete) one turn
    GET  /api/voice/timeline?session_id=&limit=                            turns + p50/p90, newest first
    GET  /api/voice/timeline/aggregate?session_id=&limit=                  only the p50/p90 summary

See src/voice_timeline.py for the marks and the gaps. Rows carry timestamps and
ids only, never text.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src import voice_timeline as vt
from src.auth_helpers import get_current_user
from src.owner_identity import is_request_sentinel_owner


class TimelineIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str = Field(..., min_length=1, max_length=200)
    turn_id: str = Field(..., min_length=1, max_length=200)
    speech_end: Optional[float] = None
    transcript_ready: Optional[float] = None
    request_sent: Optional[float] = None
    first_token: Optional[float] = None
    tts_chunk: Optional[float] = None
    first_audio: Optional[float] = None
    stt_server_ms: Optional[float] = None
    tts_server_ms: Optional[float] = None
    server_ttft_ms: Optional[float] = None


def setup_voice_timeline_routes() -> APIRouter:
    router = APIRouter(prefix="/api/voice/timeline", tags=["voice-timeline"])

    def _owner(request: Request) -> Optional[str]:
        user = get_current_user(request)
        return None if is_request_sentinel_owner(user) else user

    @router.post("")
    async def record(body: TimelineIn, request: Request) -> Dict[str, Any]:
        raw = body.model_dump(exclude={"session_id", "turn_id"}, exclude_none=True)
        try:
            turn = await asyncio.to_thread(vt.record_turn, body.session_id, body.turn_id, raw, _owner(request) or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"turn": turn}

    @router.get("")
    async def timeline(request: Request, session_id: str = "", limit: int = Query(50, ge=1, le=500)) -> Dict[str, Any]:
        return await asyncio.to_thread(vt.session_report, session_id or None, limit, _owner(request))

    @router.get("/aggregate")
    async def summary(request: Request, session_id: str = "", limit: int = Query(50, ge=1, le=500)) -> Dict[str, Any]:
        turns = await asyncio.to_thread(vt.list_turns, session_id or None, limit, _owner(request))
        return {"session_id": session_id or None, **vt.aggregate(turns)}

    return router
