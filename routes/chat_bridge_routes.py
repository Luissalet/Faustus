"""routes/chat_bridge_routes.py -- status and a token check for the chat bridges.

GET  /api/chat-bridges/telegram/status   whether the poller runs, the bot it
                                         found, the last error, the allowed
                                         chats and the conversations they map to
POST /api/chat-bridges/telegram/test     `getMe` with the saved token

Both are admin only. Neither returns the token (not even masked), and neither
changes a setting: turning the bridge on is the normal settings save.
"""
import logging

from fastapi import APIRouter, Request

from core.middleware import require_admin
from src import chat_bridges

logger = logging.getLogger(__name__)


def setup_chat_bridge_routes():
    router = APIRouter(prefix="/api/chat-bridges")

    @router.get("/telegram/status")
    async def telegram_status(request: Request):
        require_admin(request)
        return chat_bridges.get_bridge().status()

    @router.post("/telegram/test")
    async def telegram_test(request: Request):
        require_admin(request)
        return await chat_bridges.check_token()

    return router
