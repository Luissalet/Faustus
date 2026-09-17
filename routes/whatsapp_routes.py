# routes/whatsapp_routes.py
"""`/api/whatsapp` — the WhatsApp screen backend (src/whatsapp_bridge.py).

Reads are `require_admin`; everything that starts a process, unlinks the
account or **sends a message** is `require_human` (the agent's internal
token is refused — the agent sends through its own tool, which carries an
approval card).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core.middleware import require_admin, require_human
from src import whatsapp_bridge as wa

logger = logging.getLogger(__name__)


class SendBody(BaseModel):
    to: str
    text: str


class ChatBody(BaseModel):
    chat: Optional[str] = None


class HistoryBody(BaseModel):
    chat: str
    count: int = 50


class IdBody(BaseModel):
    id: str


def _owner(request: Request) -> str:
    return getattr(request.state, "current_user", None) or ""


def setup_whatsapp_routes() -> APIRouter:
    router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

    @router.get("/status")
    async def get_status(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return await asyncio.to_thread(wa.status)

    @router.post("/start")
    async def start(request: Request) -> Dict[str, Any]:
        require_human(request)
        return await asyncio.to_thread(wa.start, owner=_owner(request))

    @router.post("/stop")
    async def stop(request: Request) -> Dict[str, Any]:
        require_human(request)
        return await asyncio.to_thread(wa.stop)

    @router.post("/logout")
    async def logout(request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.logout)
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))

    @router.get("/chats")
    async def list_chats(request: Request, limit: int = 50) -> Dict[str, Any]:
        require_admin(request)
        try:
            return {"chats": await asyncio.to_thread(wa.chats, limit)}
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))

    @router.get("/messages")
    async def list_messages(request: Request, chat: Optional[str] = None, hours: float = 48,
                            limit: int = 200, unread: int = 0) -> Dict[str, Any]:
        require_admin(request)
        try:
            rows = await asyncio.to_thread(wa.messages, chat, since_hours=hours, limit=limit, unread_only=bool(unread))
            return {"messages": rows}
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))

    @router.get("/contacts")
    async def list_contacts(request: Request, q: str = "") -> Dict[str, Any]:
        require_admin(request)
        try:
            return {"contacts": await asyncio.to_thread(wa.contacts, q)}
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))

    @router.post("/send")
    async def send(body: SendBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.send, body.to, body.text)
        except wa.BridgeError as exc:
            raise HTTPException(409 if str(exc).startswith("ambiguous") else 503, str(exc))

    @router.get("/avatar")
    async def avatar(request: Request, jid: str) -> Response:
        require_admin(request)
        try:
            got = await asyncio.to_thread(wa.avatar, jid)
        except wa.BridgeError:
            got = None
        if not got:
            raise HTTPException(404, "no picture")
        return Response(content=got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/media/{name}")
    async def media(request: Request, name: str) -> Response:
        require_admin(request)
        try:
            got = await asyncio.to_thread(wa.media, name)
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))
        if not got:
            raise HTTPException(404, "no such media")
        return Response(content=got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=86400"})

    @router.post("/history")
    async def history(body: HistoryBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            return await asyncio.to_thread(wa.history, body.chat, body.count)
        except wa.BridgeError as exc:
            raise HTTPException(409 if str(exc).startswith("ambiguous") else 503, str(exc))

    @router.post("/transcribe")
    async def transcribe(body: IdBody, request: Request) -> Dict[str, Any]:
        """Text of one voice note through Faustus's speech provider (cached)."""
        require_admin(request)
        cached = wa.cached_transcript(body.id)
        if cached is not None:
            return {"id": body.id, "text": cached, "cached": True}
        try:
            rows = await asyncio.to_thread(wa.messages, None, since_hours=24 * 365, limit=2000)
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))
        row = next((m for m in rows if m.get("id") == body.id), None)
        if not row:
            raise HTTPException(404, "no such message")
        if row.get("kind") != "audio" or not row.get("media"):
            raise HTTPException(400, "not a voice note, or its audio was not pulled")
        try:
            text = await asyncio.to_thread(wa.transcribe, row)
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))
        if text is None:
            raise HTTPException(503, "no speech-to-text provider available (Settings → Voice)")
        return {"id": body.id, "text": text, "cached": False}

    @router.post("/mark-read")
    async def mark_read(body: ChatBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            return await asyncio.to_thread(wa.mark_read, body.chat)
        except wa.BridgeError as exc:
            raise HTTPException(503, str(exc))

    return router
