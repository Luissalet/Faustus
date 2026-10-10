"""routes/agent_mailbox_routes.py -- the mailbox between agents (#235).

POST /api/agent-mailbox/send                     leave a message for a chat
GET  /api/agent-mailbox/{session_id}/inbox       what other agents sent this chat (+ waiting/unread counts)
GET  /api/agent-mailbox/{session_id}/outbox      what this chat sent, with each message's state
POST /api/agent-mailbox/{session_id}/read        mark inbox messages as read (all without ``ids``)

Everything is scoped to the caller's owner. The sender of a message sent through
the API is an OUTSIDE CALLER (``external:<label>``) unless ``from_session`` names
one of the caller's own chats. A refusal (hop limit, unknown chat, mailbox off...)
is an HTTP error that carries the reason - the sender always learns it. Nothing
here touches approvals or settings: see src/agent_mailbox.py.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from routes.session_routes import _verify_session_owner
from src import agent_mailbox
from src.auth_helpers import effective_user, require_user

_STATUS = {"empty": 400, "no_recipient": 400, "bad_sender": 400, "unknown_recipient": 404, "self": 400,
           "disabled": 403, "hop_limit": 409, "inbox_full": 429}


def setup_agent_mailbox_routes() -> APIRouter:
    router = APIRouter(tags=["agent-mailbox"])

    @router.post("/api/agent-mailbox/send")
    async def send_message(request: Request, _u: str = Depends(require_user)):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "A JSON body {to, body, urgent?, reply_to?} is required")
        owner = str(effective_user(request) or "")
        from_session = str(body.get("from_session") or "").strip()
        if from_session:
            _verify_session_owner(request, from_session)
            sender = agent_mailbox.Sender.session(from_session)
        else:
            sender = agent_mailbox.Sender.external(str(body.get("sender") or ""))
        result = agent_mailbox.deliver(
            owner, sender, str(body.get("to") or ""), str(body.get("body") or ""),
            urgent=bool(body.get("urgent")), reply_to=str(body.get("reply_to") or ""))
        if not result.get("ok"):
            return JSONResponse(status_code=_STATUS.get(str(result.get("reason")), 409), content=result)
        return result

    @router.get("/api/agent-mailbox/{session_id}/inbox")
    async def get_inbox(request: Request, session_id: str, state: str = "", limit: int = 50,
                        _u: str = Depends(require_user)) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        return agent_mailbox.inbox(str(effective_user(request) or ""), session_id, state=state, limit=limit)

    @router.get("/api/agent-mailbox/{session_id}/outbox")
    async def get_outbox(request: Request, session_id: str, state: str = "", limit: int = 50,
                         _u: str = Depends(require_user)) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        return agent_mailbox.outbox(str(effective_user(request) or ""), session_id, state=state, limit=limit)

    @router.post("/api/agent-mailbox/{session_id}/read")
    async def read_messages(request: Request, session_id: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        ids = body.get("ids") if isinstance(body, dict) else None
        changed = agent_mailbox.mark_read(str(effective_user(request) or ""), session_id,
                                          [str(i) for i in ids] if isinstance(ids, list) else None)
        return {"session_id": session_id, "marked": changed, **agent_mailbox.counts_for(session_id)}

    return router


__all__ = ["setup_agent_mailbox_routes"]
