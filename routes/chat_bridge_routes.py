"""routes/chat_bridge_routes.py -- status and a token check for the chat bridges.

GET  /api/chat-bridges/telegram/status   whether the poller runs, the bot it
                                         found, the last error, the allowed
                                         chats and the conversations they map to
POST /api/chat-bridges/telegram/test     `getMe` with the saved token
GET  /api/chat-bridges/xchat/status      the X self-chat bridge: configured, running, what is missing
POST /api/chat-bridges/xchat/setup       register Faustus's X Chat encryption identity on the account

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

    # X direct messages, your chat with yourself (src/chat_bridges/xchat_bridge.py).
    @router.get("/xchat/status")
    async def xchat_status(request: Request):
        require_admin(request)
        from src.chat_bridges import xchat_bridge
        return xchat_bridge.get_bridge().status()

    @router.post("/xchat/setup")
    async def xchat_setup(request: Request):
        """Faustus's encryption identity on the X account: generated with the Chat XDK,
        its public keys registered on the account. Run once, by the owner."""
        require_admin(request)
        from fastapi import HTTPException
        from src.chat_bridges import xchat_bridge
        try:
            result = await xchat_bridge.setup_identity()
        except xchat_bridge.XChatError as exc:
            raise HTTPException(400, str(exc))
        await xchat_bridge.apply_settings()
        return result

    return router
