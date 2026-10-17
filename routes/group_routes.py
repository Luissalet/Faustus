"""Owner-scoped native group API; the same coordinator serves UI and MCP."""
from typing import Any, Literal
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import _auth_disabled, effective_user, require_user


class GroupCreate(BaseModel):
    participants: list[dict[str, Any]]
    mode: Literal["parallel", "round-robin"] = "round-robin"
    executionMode: Literal["chat", "agent"] = "agent"
    name: str = ""
    workspace: str = ""
    space_id: str = "ecosystem"
    # The room's project: members work in it (instructions, folders, board).
    project_id: str = ""
    # Follow-up rounds in which the members answer each other (0 = none).
    discussionRounds: int = Field(default=3, ge=0, le=12)


class GroupMessage(BaseModel):
    message: str
    request_id: str = ""
    mentions: list[str] = Field(default_factory=list)


class GroupUpdate(BaseModel):
    name: str | None = None
    participants: list[dict[str, Any]] | None = None
    mode: Literal['parallel', 'round-robin'] | None = None
    executionMode: Literal['chat', 'agent'] | None = None
    workspace: str | None = None
    discussionRounds: int | None = Field(default=None, ge=0, le=12)
    # Links a room that has no project yet; a room never changes project.
    projectId: str | None = None
    expected_revision: int | None = None


class GroupAnswer(BaseModel):
    message: str = ""
    answer: dict[str, Any] = Field(default_factory=dict)


def group_owner(request):
    require_user(request)
    if _auth_disabled():
        return None
    from core.middleware import is_agent_channel
    if is_agent_channel(request):
        from src.owner_identity import REQUEST_SENTINEL_OWNERS
        configured = request.headers.get('X-Faustus-Group-Owner', '').strip()
        if configured and configured not in REQUEST_SENTINEL_OWNERS:
            return configured
        from core.database import SessionLocal, Session
        with SessionLocal() as db:
            owners = {r[0] for r in db.query(Session.owner).distinct().all()
                      if r[0] and r[0] not in REQUEST_SENTINEL_OWNERS}
        if len(owners) == 1:
            return next(iter(owners))
        raise HTTPException(400, 'Set FAUSTUS_GROUPS_OWNER for the authenticated native group client')
    return effective_user(request)


def setup_group_routes(coordinator):
    router = APIRouter(prefix="/api/groups", tags=["groups"])

    def owner(request):
        return group_owner(request)

    def fail(error):
        from src.hoard_link.coordination.council_store import RevisionConflict
        if isinstance(error, RevisionConflict):
            raise HTTPException(409, str(error)) from error
        if isinstance(error, KeyError):
            raise HTTPException(404, "Group or participant session not found") from error
        raise HTTPException(400, str(error)) from error

    @router.get("/config")
    def config(request: Request):
        user = owner(request)
        from services.hoard_copilot import global_model
        from src.settings import get_setting
        model = global_model(user)
        endpoint_id = get_setting("default_endpoint_id", "")
        configured_model = model["model"] or get_setting("default_model", "")
        route = {"id": endpoint_id + "::" + configured_model, "model": configured_model,
                 "endpointId": endpoint_id, "endpointName": "Global model", "endpointUrl": model["endpoint_url"], "kind": "global",
                 "missing": not bool(model["endpoint_url"])}
        return {"defaultRoute": route if configured_model else None,
                "executionModes": ["chat", "agent"], "modes": ["parallel", "round-robin"]}

    @router.get("")
    def list_groups(request: Request, space_id: str = ""):
        args = {"space_id": space_id} if space_id else {}
        return {"groups": coordinator.store.list(owner(request), **args)}

    @router.post("")
    def create(request: Request, body: GroupCreate):
        user = owner(request)
        from routes.session_routes import _reject_raw_endpoint_url_for_non_admin
        try:
            for p in body.participants:
                route = p.get("route") or {}
                _reject_raw_endpoint_url_for_non_admin(request, user, route.get("endpointId", ""), route.get("endpointUrl", ""))
            project_id = body.project_id.strip()
            if project_id:
                from services.projects import get_store as project_store
                if not project_store().get(project_id, owner=user):
                    raise KeyError("Project not found")
            definition = coordinator.transcript.create(body.participants, body.mode, user, name=body.name,
                workspace=body.workspace, execution_mode=body.executionMode, project_id=project_id)
            definition["space_id"] = body.space_id
            definition["discussionRounds"] = body.discussionRounds
            return coordinator.adopt(definition, user)
        except (KeyError, ValueError) as error:
            fail(error)

    @router.post("/adopt")
    def adopt(request: Request, body: dict[str, Any]):
        try:
            return coordinator.adopt(body, owner(request))
        except (KeyError, ValueError) as error:
            fail(error)

    @router.get("/{group_id}")
    def snapshot(request: Request, group_id: str):
        try:
            return coordinator.snapshot(group_id, owner(request))
        except (KeyError, ValueError) as error:
            fail(error)

    @router.post("/{group_id}/messages")
    async def send(request: Request, group_id: str, body: GroupMessage):
        try:
            return await coordinator.submit(group_id, owner(request), body.message, body.request_id or uuid.uuid4().hex, body.mentions)
        except (KeyError, ValueError) as error:
            fail(error)

    @router.patch('/{group_id}')
    def update(request: Request, group_id: str, body: GroupUpdate):
        try:
            from routes.session_routes import _reject_raw_endpoint_url_for_non_admin
            user = owner(request)
            for participant in body.participants or []:
                route = participant.get('route') or {}
                _reject_raw_endpoint_url_for_non_admin(request, user, route.get('endpointId', ''), route.get('endpointUrl', ''))
            changes = body.model_dump(exclude_none=True, exclude={'expected_revision'})
            if changes.get('projectId'):
                from services.projects import get_store as project_store
                if not project_store().get(changes['projectId'], owner=user):
                    raise KeyError("Project not found")
            return coordinator.update(group_id, user, changes, body.expected_revision)
        except (KeyError, ValueError) as error:
            fail(error)

    @router.post("/{group_id}/stop")
    async def stop(request: Request, group_id: str):
        try:
            return await coordinator.stop(group_id, owner(request))
        except (KeyError, ValueError) as error:
            fail(error)

    @router.post("/{group_id}/resume")
    async def resume(request: Request, group_id: str, body: GroupAnswer):
        try:
            if body.answer.get("tool_approval_decision"):
                # Keep the existing native distinction between a user's decision
                # and a model attempting to approve its own pending action.
                from core.middleware import require_human
                require_human(request)
            return await coordinator.resume(group_id, owner(request), body.message, body.answer)
        except (KeyError, ValueError) as error:
            fail(error)

    @router.post("/{group_id}/archive")
    async def archive(request: Request, group_id: str):
        try:
            return await coordinator.archive(group_id, owner(request))
        except (KeyError, ValueError) as error:
            fail(error)

    return router
