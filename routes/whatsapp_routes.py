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

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from core.middleware import require_admin, require_human
from src import whatsapp_bridge as wa

logger = logging.getLogger(__name__)


class SendBody(BaseModel):
    to: str
    text: str = ""
    quote: Optional[str] = None
    mentions: Optional[list] = None


class ReactBody(BaseModel):
    id: str
    emoji: str = ""


class ForwardBody(BaseModel):
    id: str
    to: str


class EditBody(BaseModel):
    id: str
    text: str


class TypingBody(BaseModel):
    chat: str
    state: str = "composing"


class AssistBody(BaseModel):
    chat: str
    task: str = "summarize"
    instruction: str = ""
    hours: float = 48


class ChatBody(BaseModel):
    chat: Optional[str] = None


class HistoryBody(BaseModel):
    chat: str
    count: int = 50


class IdBody(BaseModel):
    id: str


def _code(exc: Exception) -> int:
    msg = str(exc)
    if msg.startswith("ambiguous"):
        return 409
    if "cannot quote" in msg or "no longer" in msg:
        return 409
    if msg.startswith("only your own") or "own messages" in msg:
        return 403
    if "not found" in msg or msg.startswith("unknown"):
        return 404
    return 503


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
        if not (body.text or "").strip():
            raise HTTPException(400, "text is required")
        try:
            return await asyncio.to_thread(wa.send, body.to, body.text, quote=body.quote, mentions=body.mentions)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...), to: str = Form(...), caption: str = Form(""),
                     quote: str = Form(""), voice: str = Form("")) -> Dict[str, Any]:
        """A photo, a document or a recorded voice note to one chat."""
        require_human(request)
        from src.upload_limits import read_upload_limited
        data = await read_upload_limited(file, 25 * 1024 * 1024, "Attachment")
        if not data:
            raise HTTPException(400, "empty file")
        mime = file.content_type or "application/octet-stream"
        try:
            return await asyncio.to_thread(wa.send_file, to, data, mime, filename=file.filename or "", caption=caption,
                                           quote=quote or None, voice=voice in ("1", "true", "yes"))
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/react")
    async def react(body: ReactBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.react, body.id, body.emoji)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/delete")
    async def delete(body: IdBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.delete, body.id)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/forward")
    async def forward(body: ForwardBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.forward, body.id, body.to)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/edit")
    async def edit(body: EditBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        try:
            return await asyncio.to_thread(wa.edit, body.id, body.text)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/typing")
    async def typing(body: TypingBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            return await asyncio.to_thread(wa.typing, body.chat, body.state)
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/subscribe")
    async def subscribe(body: ChatBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            return await asyncio.to_thread(wa.subscribe, body.chat or "")
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.get("/search")
    async def search(request: Request, q: str, chat: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        require_admin(request)
        try:
            return {"messages": await asyncio.to_thread(wa.search, q, chat, limit)}
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

    @router.post("/assist")
    async def assist(body: AssistBody, request: Request) -> Dict[str, Any]:
        """Ask Faustus about one chat (summary, a draft in the owner's voice,
        a translation, anything): text back, nothing sent."""
        require_admin(request)
        from src import whatsapp_tools
        try:
            return await whatsapp_tools.assist(_owner(request), body.chat, body.task, body.instruction, body.hours)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except wa.BridgeError as exc:
            raise HTTPException(_code(exc), str(exc))

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
