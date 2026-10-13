"""Native channel connection API backed by existing Integration credentials."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import channel_connections as channels


class ConnectionRequest(BaseModel):
    provider: str
    integration_id: str
    name: str = ""
    connection_id: str = ""


class OperationRequest(BaseModel):
    connection_id: str
    operation: str
    channel_id: str = ""
    parent_id: str = ""
    cursor: str = ""
    limit: int = 100
    text: str = ""
    message_id: str = ""
    reaction: str = ""
    transaction_id: str = ""


def setup_channel_connection_routes():
    router = APIRouter(prefix="/api/channel-connections", tags=["channel-connections"])

    @router.get("/providers")
    def providers(request: Request):
        require_admin(request)
        return {"providers": [{"id": key, **value} for key, value in channels.PROVIDERS.items()]}

    @router.get("")
    def connections(request: Request):
        require_admin(request)
        return {"connections": channels.list_connections()}

    @router.post("")
    def save(request: Request, body: ConnectionRequest):
        require_admin(request)
        try:
            return channels.save_connection(**body.model_dump())
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @router.post("/run")
    async def run(request: Request, body: OperationRequest):
        require_admin(request)
        try:
            if body.operation == "spaces":
                return await channels.list_spaces(body.connection_id, body.parent_id, body.cursor, body.limit)
            if body.operation == "history":
                return await channels.history(body.connection_id, body.channel_id, body.cursor, body.limit)
            if body.operation in ("send", "edit"):
                message = channels._id(body.message_id, "message_id") if body.operation == "edit" else ""
                return await channels.write_message(body.connection_id, body.channel_id, body.text,
                                                    message, body.transaction_id)
            if body.operation == "react":
                return await channels.react(body.connection_id, body.channel_id, body.message_id,
                                            body.reaction, body.transaction_id)
            raise ValueError("Supported operations: spaces, history, send, edit, react")
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    return router
