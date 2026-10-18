"""Spheres of the family hub, proxied for the browser.

`GET /api/hoard/spheres` and `POST /api/hoard/spheres/active {id}` forward to
the Hoard Hub through `src/hoard_hub.py`, so Studio never calls the hub
cross-origin. A hub that is down or refuses answers 200 with
``{"ok": false, "error": ...}``: the header chip hides itself on that, and
nothing else in Faustus depends on the hub being there.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
from pydantic import BaseModel, Field, ConfigDict, StrictInt

from core.middleware import require_admin
from src import hoard_hub


class ActiveSphere(BaseModel):
    id: str = Field(default="", max_length=64)


class ProjectTaskCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    body: str = Field('', max_length=20000)
    acceptance: str = Field('', max_length=20000)
    priority: StrictInt = Field(2, ge=0, le=3)
    kind: Literal['feature', 'bug', 'research', 'review', 'chore', 'eval', 'docs'] = 'feature'
    owner: str | None = Field(None, max_length=41)
    note: str = Field('', max_length=20000)
    repo: str = Field('', max_length=120)
    request_id: str | None = Field(None, pattern=r'^[a-zA-Z0-9-]{1,64}$')


class ProjectTaskEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_etag: str = Field(min_length=1, max_length=64)
    title: str | None = Field(None, max_length=200)
    body: str | None = Field(None, max_length=20000)
    acceptance: str | None = Field(None, max_length=20000)
    priority: StrictInt | None = Field(None, ge=0, le=3)
    owner: str | None = Field(None, max_length=41)
    note: str = Field('', max_length=20000)


# The Hub's own board groups: what is waiting, what is moving, what waits for
# a reviewer, what is finished.
BOARD_COLUMNS = {
    'open': 'open',
    'claimed': 'in_progress', 'in_progress': 'in_progress', 'blocked': 'in_progress', 'changes': 'in_progress',
    'review': 'review',
    'approved': 'done', 'done': 'done',
}
_TASK_FIELDS = ('id', 'title', 'status', 'owner', 'reviewer', 'kind', 'priority', 'repo', 'created', 'updated',
                'claimed_at', 'submitted_at', 'closed_at', 'review_state', 'thread_id', 'created_by')


def compact_board(board, *, done_limit=40):
    columns = {'open': [], 'in_progress': [], 'review': [], 'done': []}
    dropped = 0
    for task in board.get('tasks') or ():
        column = BOARD_COLUMNS.get(str(task.get('status') or ''))
        if column is None:
            dropped += 1
            continue
        row = {key: task.get(key) for key in _TASK_FIELDS}
        row['body'] = str(task.get('body') or '')[:600]
        activity = task.get('activity') if isinstance(task.get('activity'), dict) else {}
        row['activity'] = activity.get('state')
        columns[column].append(row)
    for name, rows in columns.items():
        if name == 'done':
            rows.sort(key=lambda r: r.get('closed_at') or r.get('updated') or 0, reverse=True)
        else:
            rows.sort(key=lambda r: (r.get('priority') if isinstance(r.get('priority'), int) else 2, -(r.get('updated') or 0)))
    counts = board.get('counts') if isinstance(board.get('counts'), dict) else {}
    # The Hub sends only the recent finished tasks; its counts know them all.
    done_total = max(len(columns['done']), int(counts.get('done') or 0) + int(counts.get('approved') or 0))
    columns['done'] = columns['done'][:done_limit]
    agents = [{k: a.get(k) for k in ('id', 'state', 'doing', 'stale')} for a in board.get('agents') or ()
              if isinstance(a, dict)]
    return {'ok': True, 'space_id': board.get('space_id'), 'columns': columns, 'counts': board.get('counts') or {},
            'done_total': done_total, 'dropped': dropped, 'agents': agents}


def setup_hoard_hub_routes() -> APIRouter:
    router = APIRouter(tags=["hoard-hub"])

    def project_for_tasks(project_id, request):
        require_admin(request)
        from services.projects import get_store
        from src.auth_helpers import effective_user
        project = get_store().get(project_id, effective_user(request))
        if not project:
            raise HTTPException(404, 'Project not found')
        return project

    async def task_call(function, *args):
        from src.project_tasks import TaskError
        try:
            return await asyncio.to_thread(function, *args)
        except TaskError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.post('/api/projects/{project_id}/tasks/prepare')
    async def prepare_tasks(project_id: str, request: Request):
        from src.project_tasks import prepare
        return await task_call(prepare, project_for_tasks(project_id, request))

    @router.get('/api/projects/{project_id}/tasks')
    async def project_tasks(project_id: str, request: Request):
        from src.project_tasks import listing
        return await task_call(listing, project_for_tasks(project_id, request))

    @router.post('/api/projects/{project_id}/tasks')
    async def create_task(project_id: str, request: Request, body: ProjectTaskCreate):
        from src.project_tasks import command
        allowed = body.model_dump(exclude={'request_id'})
        if body.request_id:
            allowed['origin_ref'] = f"faustus:{project_id}:request:{body.request_id}"
        return await task_call(command, project_for_tasks(project_id, request), 'task_add', allowed)

    @router.get('/api/projects/{project_id}/tasks/{task_id}')
    async def project_task(project_id: str, task_id: int, request: Request):
        from src.project_tasks import command
        return await task_call(command, project_for_tasks(project_id, request), 'task', {'task_id': task_id})

    @router.patch('/api/projects/{project_id}/tasks/{task_id}')
    async def edit_task(project_id: str, task_id: int, request: Request, body: ProjectTaskEdit):
        from src.project_tasks import command
        allowed = body.model_dump(exclude_unset=True)
        return await task_call(command, project_for_tasks(project_id, request), 'task_edit', {**allowed, 'task_id': task_id})

    @router.get('/api/hoard/coordination')
    def coordination_status():
        from src.shared_coordination import status
        return status()

    @router.get('/api/hoard/agora/spaces')
    async def coordination_spaces():
        from src.shared_coordination import call
        return await asyncio.to_thread(call, 'hub_agora_spaces')

    @router.get('/api/hoard/agora/rooms')
    async def coordination_rooms(request: Request, space_id: str = 'ecosystem'):
        from src.shared_coordination import call
        from routes.council_routes import _require_owner
        from routes.group_routes import group_owner
        return await asyncio.to_thread(call, 'hub_coordination_rooms', {'space_id': space_id,
            'owner': _require_owner(request), 'group_owner': group_owner(request) or ''})

    @router.get('/api/hoard/agora/board')
    async def coordination_board(space_id: str = 'ecosystem', done_limit: int = 40):
        """The Agora task board of one space, trimmed to what a kanban draws."""
        from src.shared_coordination import call
        result = await asyncio.to_thread(call, 'hub_agora_board', {'space_id': space_id})
        if not isinstance(result, dict) or not result.get('ok'):
            return {'ok': False, 'error': (result or {}).get('error') if isinstance(result, dict) else 'Agora unavailable'}
        return compact_board(result, done_limit=max(0, min(done_limit, 200)))

    @router.post('/api/hoard/agora/spaces')
    async def create_coordination_space(request: Request):
        require_admin(request)
        from src.shared_coordination import call
        body = await request.json()
        args = {'agent': 'faustus', 'title': body.get('title', '')}
        # A project's own agora: bound to the project and its main folder.
        for key in ('project_id', 'workspace', 'description'):
            if body.get(key):
                args[key] = str(body[key])
        return await asyncio.to_thread(call, 'hub_agora_space_create', args)

    @router.get('/api/hoard/agora/open')
    def open_coordination_board(space_id: str = 'ecosystem', embedded: bool = False):
        where = hoard_hub.hub_location()
        args = {'space_id': space_id, **({'embedded': '1'} if embedded else {})}
        return RedirectResponse(where['url'].rstrip('/') + '/agora/?' + urlencode(args))

    @router.get("/api/hoard/spheres")
    async def get_spheres() -> Dict[str, Any]:
        return await asyncio.to_thread(hoard_hub.get_spheres)

    @router.get("/api/hoard/fleet")
    async def get_fleet(request: Request) -> Dict[str, Any]:
        # Fleet labels span the machine, including other users' scheduled tasks.
        require_admin(request)
        return await asyncio.to_thread(hoard_hub.get_fleet)

    @router.get("/api/family/fleet")
    def publish_fleet(request: Request):
        # The already configured Hub credential authenticates the family observer;
        # ordinary authenticated administrators can read the same published metadata.
        # AuthMiddleware lets exactly this GET through when it carries the Hub's token; the handler checks it
        # again, so a request that reaches here without it still needs an administrator session.
        if not hoard_hub.hub_bearer_matches(request.headers.get("Authorization", "")):
            require_admin(request)
        from core.database import SessionLocal
        from src.fleet_status import build
        with SessionLocal() as db:
            return build(db)

    @router.post("/api/hoard/spheres/active")
    async def set_active_sphere(body: ActiveSphere, request: Request):
        # The sphere is machine-wide state shared by every app in the family,
        # so switching it is an admin act, like the other routes that act on
        # a plugin app.
        require_admin(request)
        result = await asyncio.to_thread(hoard_hub.set_active, body.id)
        if result.get("status") == 400:
            return JSONResponse(status_code=400, content={"ok": False, "error": result.get("error", "bad request")})
        if result.get("status") == 404:
            return JSONResponse(status_code=404, content={"ok": False, "error": result.get("error", "unknown sphere")})
        result.pop("status", None)
        return result

    return router
