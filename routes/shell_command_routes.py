"""Authenticated literal shell calls, sharing dispatch authority and bash policy."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from core.middleware import require_human
from routes.dispatch_routes import _owner
from src import shell_commands


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=65536)
    session_id: str = ""
    workspace: str = ""
    project_id: str = ""
    autonomy_preset: str = ""


class ApprovalBody(BaseModel):
    approval_id: str
    decision: str


def setup_shell_command_routes():
    router = APIRouter(prefix="/api/shell/command", tags=["shell"])

    @router.post("")
    async def execute(request: Request, body: CommandBody):
        owner = _owner(request)
        try:
            return await shell_commands.run_command(owner=owner, **body.model_dump())
        except shell_commands.ShellCommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("/pending")
    def pending(request: Request, session_id: str):
        owner = _owner(request)
        try:
            return {"approval": shell_commands.pending_for_session(session_id, owner)}
        except shell_commands.ShellCommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.post("/approval")
    async def approve(request: Request, body: ApprovalBody):
        require_human(request)
        owner = _owner(request)
        try:
            return await shell_commands.answer_approval(owner=owner, **body.model_dump())
        except shell_commands.ShellCommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
    return router
