"""Owner-managed project coordination, exposed through the normal API/MCP."""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_admin
from src.auth_helpers import effective_user
from src.coordination.bindings import Binding, ProjectTemplate, CoordinationError, StaleBinding, UnknownOutcome, OPERATIONS
from src.coordination.runtime import client, connector_fingerprint, get_store, resolve_grant, storage_owner, validate


class SaveBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(default='ecosystem', max_length=64)
    actor: str | None = Field(None, max_length=48)
    operations: list[str] = Field(default_factory=lambda: sorted(OPERATIONS), max_length=100)
    resources: list[str] = Field(default_factory=list, max_length=100)
    repos: list[str] = Field(default_factory=list, max_length=100)
    thread_ids: list[int] = Field(default_factory=list, max_length=100)
    id: str | None = Field(None, max_length=100)
    expected_revision: int | None = Field(None, ge=1)
    active: bool = True


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["poll", "detail", "task", "board", "heartbeat", "add", "claim", "checkpoint", "post", "submit", "review", "done", "lock", "unlock"]
    arguments: dict = Field(default_factory=dict)
    read_only: bool = False


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt: str | None = Field(None, max_length=100)
    task_receipt: str | None = Field(None, max_length=100)
    thread_ids: list[int] = Field(default_factory=list, max_length=100)
    binding_revision: int = Field(ge=1)


def setup_coordination_routes():
    router = APIRouter(prefix="/api/coordination", tags=["coordination"], dependencies=[Depends(require_admin)])

    def grant(request, session_id, read_only=False):
        return resolve_grant(effective_user(request), session_id, read_only=read_only)

    def fail(exc):
        detail = {"error": str(exc), "outcome_unknown": True, "non_retryable": True} if isinstance(exc, UnknownOutcome) else str(exc)
        return HTTPException(409 if isinstance(exc, (StaleBinding, UnknownOutcome)) else 400, detail)

    def project(request, project_id):
        from services.projects import get_store as projects
        row = projects().get(project_id, effective_user(request))
        if not row:
            raise HTTPException(404, "Project not found")
        return row

    @router.get("/projects/{project_id}")
    async def get_project_binding(request: Request, project_id: str):
        def get():
            row = project(request, project_id)
            template = get_store().template(storage_owner(effective_user(request)), project_id)
            usable, unavailable = False, None
            if template and template.active:
                try:
                    if row.get("enabled") is False:
                        raise CoordinationError("project is disabled")
                    allowed = row.get("connectors")
                    if allowed is not None and (not isinstance(allowed, list) or template.connector_id not in allowed):
                        raise CoordinationError("this project does not allow the coordination connector")
                    from src.tool_utils import get_mcp_manager
                    if connector_fingerprint(template.connector_id) != template.connector_fingerprint:
                        raise CoordinationError("connector configuration changed; save coordination again")
                    manager = get_mcp_manager()
                    if manager is None or manager.get_server_status(template.connector_id).get("status") != "connected":
                        raise CoordinationError("coordination connector is not connected")
                    usable = True
                except CoordinationError as exc:
                    unavailable = str(exc)
            return {"template": template.serialise() if template else None, "usable": usable, "unavailable": unavailable}
        return await asyncio.to_thread(get)

    @router.put("/projects/{project_id}")
    async def save_project_binding(request: Request, project_id: str, body: SaveBinding):
        def save():
            from dataclasses import replace
            row = project(request, project_id)
            owner = storage_owner(effective_user(request))
            store = get_store()
            previous = store.template(owner, project_id)
            if body.id and (not previous or body.id != previous.id):
                raise CoordinationError("project coordination identity does not match")
            if body.actor is not None:
                raise CoordinationError("project coordination identity is selected by the product")
            if previous and body.active is False:
                return store.save_template(replace(previous, active=False), expected_revision=body.expected_revision).serialise()
            if row.get("enabled") is False:
                raise CoordinationError("project is disabled")
            allowed = row.get("connectors")
            if allowed is not None and (not isinstance(allowed, list) or body.connector_id not in allowed):
                raise CoordinationError("this project does not allow the coordination connector")
            identifier = previous.id if previous else uuid.uuid4().hex
            template = ProjectTemplate(id=identifier, owner=owner, project_id=project_id,
                connector_id=body.connector_id, actor=previous.actor if previous else "faustus-" + identifier[:12],
                operations=tuple(body.operations), resources=tuple(body.resources), repos=tuple(body.repos),
                thread_ids=tuple(body.thread_ids), active=body.active, space_id=body.space_id,
                connector_fingerprint=connector_fingerprint(body.connector_id))
            return store.save_template(template, expected_revision=body.expected_revision).serialise()
        try:
            return {"template": await asyncio.to_thread(save)}
        except CoordinationError as exc:
            raise fail(exc) from exc

    @router.get("/projects/{project_id}/choices")
    async def project_choices(request: Request, project_id: str, connector_id: str):
        from src.coordination.project_config import choices
        from src.tool_utils import get_mcp_manager
        try:
            row = await asyncio.to_thread(project, request, project_id)
            return await choices(row, connector_id, get_mcp_manager())
        except (CoordinationError, asyncio.TimeoutError) as exc:
            return {"choices": [], "unavailable": str(exc) or "repository catalogue timed out"}

    @router.get("/{session_id}")
    async def get_binding(request: Request, session_id: str):
        try:
            from routes.session_routes import _verify_session_owner
            await asyncio.to_thread(_verify_session_owner, request, session_id)
            store = get_store()
            bindings = await asyncio.to_thread(store.session_bindings, storage_owner(effective_user(request)), session_id)
            selected = next((b for b in bindings if b.active), bindings[0] if bindings else None)
            from services.projects import project_for_session
            attached = await asyncio.to_thread(project_for_session, session_id, effective_user(request))
            template = store.template(storage_owner(effective_user(request)), attached["id"]) if attached else None
            usable, unavailable = False, None
            if (selected and selected.active) or (not selected and template and template.active):
                try:
                    if selected:
                        store.live(selected)
                        from src.tool_utils import get_mcp_manager
                        await asyncio.to_thread(validate, selected, owner=effective_user(request), session_id=session_id, manager=get_mcp_manager())
                    else:
                        from src.tool_utils import get_mcp_manager
                        await asyncio.to_thread(validate, template.for_session(session_id), owner=effective_user(request), session_id=session_id, manager=get_mcp_manager())
                    usable = True
                except CoordinationError as exc:
                    unavailable = str(exc)
            return {"binding": selected.serialise() if selected else None, "usable": usable,
                    "template": template.serialise() if template else None,
                    "unavailable": unavailable, "cursor": store.cursor(selected) if selected and usable else 0,
                    "pending": store.pending(selected) if selected and usable else []}
        except CoordinationError as exc:
            raise fail(exc) from exc

    @router.put("/{session_id}")
    async def save_binding(request: Request, session_id: str, body: SaveBinding):
        def save():
            from services.projects import project_for_session
            from src.tool_utils import get_mcp_manager
            from routes.session_routes import _verify_session_owner
            _verify_session_owner(request, session_id)
            owner = effective_user(request)
            store = get_store()
            previous = store.get(storage_owner(owner), body.id) if body.id else None
            if previous and previous.session_id != session_id:
                raise CoordinationError("binding belongs to another chat")
            if previous and body.active is False:
                from dataclasses import replace
                return store.save(replace(previous, active=False), expected_revision=body.expected_revision).serialise()
            project = project_for_session(session_id, owner)
            if not project:
                raise CoordinationError("attach this chat to a project before configuring coordination")
            identifier = body.id or uuid.uuid4().hex
            actor = body.actor or (previous.actor if previous else "faustus-" + identifier[:12])
            binding = Binding(id=identifier, owner=storage_owner(owner), project_id=project["id"],
                              session_id=session_id, connector_id=body.connector_id, actor=actor,
                              operations=tuple(body.operations), resources=tuple(body.resources), repos=tuple(body.repos),
                              thread_ids=tuple(body.thread_ids), active=body.active, space_id=body.space_id,
                              connector_fingerprint=connector_fingerprint(body.connector_id))
            # Revocation must work even when the connector is offline or removed.
            if body.active:
                validate(binding, owner=owner, session_id=session_id, manager=get_mcp_manager())
            return store.save(binding, expected_revision=body.expected_revision).serialise()
        try:
            return {"binding": await asyncio.to_thread(save)}
        except CoordinationError as exc:
            raise fail(exc) from exc

    @router.post("/{session_id}/actions")
    async def run_action(request: Request, session_id: str, body: Action):
        try:
            current = await asyncio.to_thread(grant, request, session_id, body.read_only)
            data = await client(current).execute(body.action, body.arguments)
            # API clients explicitly confirm delivery; a disconnected HTTP response
            # leaves a fetched message/task pending rather than acknowledging it.
            return {"result": data, "binding_revision": current.binding.revision}
        except CoordinationError as exc:
            raise fail(exc) from exc

    @router.post("/{session_id}/delivery")
    async def confirm_delivery(request: Request, session_id: str, body: Delivery):
        try:
            current = await asyncio.to_thread(grant, request, session_id)
            if body.binding_revision != current.binding.revision:
                raise StaleBinding("binding changed before delivery confirmation")
            if bool(body.receipt) == bool(body.task_receipt):
                raise CoordinationError("confirm exactly one message or task receipt")
            if body.receipt:
                data = await asyncio.to_thread(current.store.confirm_delivery, current.binding, body.receipt, threads=body.thread_ids)
            else:
                data = {"task_id": await asyncio.to_thread(current.store.confirm_task_delivery, current.binding, body.task_receipt)}
            return {"delivery": data, "hub_acknowledged": False}
        except CoordinationError as exc:
            raise fail(exc) from exc

    return router
