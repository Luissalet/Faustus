"""Projects API — CRUD over projects plus read/write access to their on-disk
memory.

Admin-gated throughout, for the same reason ``workspace_routes`` is: a project
carries a host filesystem path, and the memory endpoints read and write real
files under it. A caller who is not allowed to use the file/shell tools must not
be able to reach those paths through this router either.

``GET /{project_id}/objectives`` also answers in robot mode
(``?robot=1`` / ``?format=toon``, src/robot_envelope.py) for a coordinating
model, carrying the LEAN projection of the dashboard
(src/robot_projection.py): one flat row per objective with its impact score
folded in and its deps joined into ``blocked_by``, plus the edges and the
audit tail as their own tables. Without a query parameter the dashboard
answer is unchanged.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import robot_envelope as robot
from src import robot_projection as lean
from src.auth_helpers import effective_user, storage_owner_for_request
from services.projects import (
    MAX_INSTRUCTIONS,
    MAX_MEMORY_FILE,
    ProjectError,
    get_store,
)

logger = logging.getLogger(__name__)


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    folder: str = Field("", max_length=80, description="Sidebar chat folder; defaults to name")
    workspace: str = Field("", max_length=4096, description="Absolute path to the project folder")
    instructions: str = Field("", max_length=MAX_INSTRUCTIONS)


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    folder: Optional[str] = Field(None, max_length=80)
    workspace: Optional[str] = Field(None, max_length=4096)
    instructions: Optional[str] = Field(None, max_length=MAX_INSTRUCTIONS)
    enabled: Optional[bool] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    # Agent knobs (services.projects.AGENT_OPTION_FIELDS)
    trusted: Optional[bool] = None
    trusted_agents: Optional[bool] = None
    review_mode: Optional[bool] = None
    checkpoints: Optional[bool] = None
    run_tests: Optional[bool] = None
    test_command: Optional[str] = Field(None, max_length=400)
    review_model: Optional[str] = Field(None, max_length=200)


class MemoryWriteRequest(BaseModel):
    content: str = Field("", max_length=MAX_MEMORY_FILE)


class ContextSourceRef(BaseModel):
    """Where a typed source lives: a row id, or a filesystem path.

    ``kind`` is deliberately a plain string here. The closed vocabulary
    lives in ``src/project_context/models.py``, whose rejection names the
    field and the value it saw; restating it as an enum in the route would
    give the same mistake two different error messages.
    """

    kind: str = Field(..., min_length=1, max_length=64)
    id: str = Field("", max_length=256)
    path: str = Field("", max_length=4096)


class ContextAddRequest(BaseModel):
    """Both shapes of "add something to this project's context".

    ``{"path": "..."}`` alone is what the live UI sends and has sent since
    the endpoint existed: it still creates the same ``work_root`` item,
    with the same ten-hex id, and answers with the same ``{"item": ...}``.
    ``source`` is the typed shape and goes through
    ``ProjectContextService.attach``, which validates the source before it
    writes and deduplicates a repeated attach.

    ``path`` lost its ``min_length`` so that the two shapes can share one
    body; a request that carries neither is answered by the route, in the
    repo's refusal convention, rather than by a 422 that names a field the
    typed caller never meant to send.
    """

    path: str = Field("", max_length=4096)
    source: Optional[ContextSourceRef] = None
    label: str = Field("", max_length=512)
    role: str = Field("reference", max_length=64)
    retrieval_policy: str = Field("auto", max_length=64)
    version_policy: str = Field("latest", max_length=64)
    pinned_version: Optional[int] = Field(None, ge=1)
    tags: List[str] = Field(default_factory=list, max_length=64)
    access_mode: str = Field("read_only", max_length=64)


class ContextPatchRequest(BaseModel):
    """Policy and metadata on an existing link — ``models.PATCHABLE_FIELDS``.

    ``kind``, ``ref_id`` and ``path`` are absent on purpose: they are what
    the link IS, and editing one in place would leave everything already
    cited under the old identity pointing at a different source.
    """

    label: Optional[str] = Field(None, max_length=512)
    role: Optional[str] = Field(None, max_length=64)
    tags: Optional[List[str]] = Field(None, max_length=64)
    summary: Optional[str] = Field(None, max_length=8000)
    retrieval_policy: Optional[str] = Field(None, max_length=64)
    version_policy: Optional[str] = Field(None, max_length=64)
    pinned_version: Optional[int] = Field(None, ge=1)
    access_mode: Optional[str] = Field(None, max_length=64)
    enabled: Optional[bool] = None


class ObjectiveCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    status: Optional[str] = Field(None, max_length=20)
    priority: Optional[int] = Field(None, ge=1, le=4)
    notes: Optional[str] = Field(None, max_length=4000)
    deps: Optional[List[str]] = None


class ObjectiveUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    status: Optional[str] = Field(None, max_length=20)
    priority: Optional[int] = Field(None, ge=1, le=4)
    notes: Optional[str] = Field(None, max_length=4000)
    deps: Optional[List[str]] = None


class ObjectiveDeltasRequest(BaseModel):
    deltas: List[Dict[str, Any]] = Field(..., min_length=1, max_length=50)


def setup_project_routes() -> APIRouter:
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    def _get_or_404(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = get_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @router.get("")
    def list_projects(request: Request, _admin: None = Depends(require_admin)) -> List[Dict[str, Any]]:
        return get_store().list(effective_user(request))

    @router.post("")
    def create_project(
        payload: ProjectCreateRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        try:
            return get_store().create(
                name=payload.name,
                folder=payload.folder,
                workspace=payload.workspace,
                instructions=payload.instructions,
                owner=effective_user(request),
            )
        except ProjectError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            logger.error("Project create failed: %s", e)
            raise HTTPException(500, "Could not save the project")

    @router.get("/{project_id}")
    def get_project(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        return _get_or_404(project_id, effective_user(request))

    @router.patch("/{project_id}")
    def update_project(
        project_id: str,
        payload: ProjectUpdateRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _get_or_404(project_id, owner)
        updates = payload.model_dump(exclude_none=True)
        try:
            updated = get_store().update(project_id, updates, owner)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        if not updated:
            raise HTTPException(404, "Project not found")
        return updated

    @router.delete("/{project_id}")
    def delete_project(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Forgets the project. The folder on disk and its memory files are
        left exactly where they are — this deletes a binding, not the work."""
        if not get_store().delete(project_id, effective_user(request)):
            raise HTTPException(404, "Project not found")
        return {"success": True}

    # ------------------------------------------------------------------
    # Session management within projects
    # ------------------------------------------------------------------

    @router.delete("/{project_id}/session/{session_id}")
    def delete_project_session(
        project_id: str,
        session_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Delete a specific session within the project."""
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        
        # Verify that the session belongs to this project
        from core.session_manager import get_session_manager_instance
        session_manager = get_session_manager_instance()
        
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session {session_id} not found")
            
        # Check if session belongs to this project's folder
        expected_folder = project.get("folder", "")
        if session.folder != expected_folder:
            raise HTTPException(400, "Session does not belong to this project")
            
        # Delete the session using the session manager
        result = session_manager.delete_session(session_id)
        if not result:
            raise HTTPException(404, "Session deletion failed")
        
        return {"success": True}

    @router.delete("/{project_id}/sessions")
    def delete_project_sessions(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Delete all sessions within the project."""
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        
        # Get session manager
        from core.session_manager import get_session_manager_instance
        session_manager = get_session_manager_instance()
        
        # Find all sessions in this project's folder using database query
        from core.database import SessionLocal, DbSession
        db = SessionLocal()
        try:
            # Find all sessions that belong to the project's folder
            project_folder = project.get("folder", "")
            db_sessions = db.query(DbSession).filter(
                DbSession.folder == project_folder,
                DbSession.archived == False
            ).all()
            
            deleted_count = 0
            
            for db_session in db_sessions:
                # Delete the session using the session manager
                if session_manager.delete_session(db_session.id):
                    deleted_count += 1
                    
            return {"success": True, "deleted_count": deleted_count}
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Typed context links
    #
    # A link is membership and policy, never content and never permission:
    # "this source belongs to this project, at this revision, under this
    # retrieval policy". Everything that decides ownership, deduplication and
    # revision lives in `src/project_context/service.py`; this router adapts
    # arguments and results and nothing else.
    #
    # Two conventions hold across the six endpoints below.
    #
    # **A rejection is a 200 with `{"ok": false, "error": {path, message}}`**,
    # the same shape `routes/contracts_routes.py` uses: the caller asked a
    # question ("can this be linked?") and got an answer. 4xx stays for a body
    # that could not be read at all -- and for the legacy `{"path": ...}`
    # form, whose 400 the live UI already renders.
    #
    # **A link that is not this owner's answers exactly like one that does not
    # exist**: 404, never 403. A 403 confirms that the id exists, which is the
    # one fact the ownership check was protecting.
    # ------------------------------------------------------------------

    #: Which field an `AttachResult.error` is about, for the refusal body.
    _ATTACH_ERROR_FIELD = {
        "owner_mismatch": "owner",
        "invalid_source": "source",
        "unsupported_kind": "source.kind",
        "unsupported": "version_policy",
        "missing": "source",
        "forbidden": "source",
        "invalid_link": "link",
    }

    def _refused(path: str, message: str) -> Dict[str, Any]:
        return {"ok": False, "error": {"path": path, "message": message}}

    def _link_owner(request: Request) -> str:
        """The owner the link service and the resolvers compare against.

        `effective_user` is the authority and stays the authority. It is None
        in the explicit no-login mode, and both the service and the resolvers
        fail closed on an empty owner -- a path has no owner column, so "is
        there an owner at all" is the whole check a filesystem resolver can
        make. Standing the reserved local owner in for it *there* is what
        keeps typed links usable on the single-user install this is built for,
        without inventing an identity when auth is on: with auth enabled
        `effective_storage_owner` returns None and the refusal stands.
        """
        return ((effective_user(request) or "").strip()
                or (storage_owner_for_request(request) or ""))

    def _outside_the_vocabulary(values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The closed vocabularies, checked at the boundary.

        `ProjectStore.normalize_link` coerces an unknown enum to the
        default instead of raising, and is right to: it reads stored data,
        and a chat must not die because a hand-edited projects.json says
        `always_full`. That liberality is wrong for a *request*. A PATCH
        that answered 200 after quietly turning `always_full` into
        `on_demand` would tell the caller it had done something it had
        not, and the caller would find out from a retrieval that never
        happened.
        """
        from src.project_context.models import (
            ACCESS_MODES, RETRIEVAL_POLICIES, ROLES, VERSION_POLICIES,
        )
        closed = {"retrieval_policy": RETRIEVAL_POLICIES, "role": ROLES,
                  "version_policy": VERSION_POLICIES,
                  "access_mode": ACCESS_MODES}
        for field, choices in closed.items():
            value = values.get(field)
            if value is not None and value not in choices:
                return _refused(field,
                                f"must be one of {list(choices)}, not {value!r}")
        return None

    def _actor(owner: str):
        from src.project_context.models import ActorRef
        return ActorRef(kind="user", name=owner)

    def _context_service():
        """The link service, talking to this router's store.

        `ProjectContextService` holds no state beyond its collaborators,
        so building one per call costs an object and keeps the store the
        one `get_store()` resolves right now.
        """
        from src.project_context.service import ProjectContextService
        return ProjectContextService(store=get_store())

    @router.get("/{project_id}/context")
    def list_context(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The project's links, typed, with the index state and the revision.

        Nothing here calls a resolver, and that is the point: a listing that
        opened every linked document to render a row would turn a page refresh
        into N reads of somebody's disk, and would be the one place where
        "the list" and "the content" could disagree. What comes back is what
        the store holds -- membership, policy, `index_status`,
        `content_revision` -- and not one byte of any source.

        `context_revision` is the project's monotonic link counter, so a
        caller can tell "nothing changed" from "I read a stale page".
        """
        owner = effective_user(request)
        _get_or_404(project_id, owner)
        store = get_store()
        return {
            "ok": True,
            "links": store.list_links(project_id, owner=owner),
            "context_revision": store.context_revision(project_id),
        }

    @router.post("/{project_id}/context")
    def add_context(
        project_id: str,
        payload: ContextAddRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Attach a source. Two shapes, one endpoint.

        With no `source`, this is the endpoint it has always been: the path
        becomes a `work_root` context item through `add_context_item`, with
        the same ten-hex id and the same `{"item": ...}` answer, and a
        rejected path is still a 400. That promise covers the error too --
        a compatibility that only held on the happy path would break the
        toast the UI shows when a path is refused.

        With a `source`, it goes through `ProjectContextService.attach`, which
        validates the source *before* it writes anything, never substitutes a
        different source with the same title, and is idempotent: attaching the
        same thing twice answers with the first link and `deduplicated: true`.
        """
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)

        if payload.source is None:
            if not payload.path.strip():
                return _refused("source", "give either a 'path' or a typed 'source'")
            try:
                item = get_store().add_context_item(project_id, payload.path, owner)
            except ProjectError as e:
                raise HTTPException(400, str(e))
            return {"item": item}

        refusal = _outside_the_vocabulary(payload.model_dump())
        if refusal is not None:
            return refusal

        from src.project_context.models import ProjectContextError

        link_owner = _link_owner(request)
        try:
            result = _context_service().attach(
                project=project,
                owner=link_owner,
                source=payload.source.model_dump(),
                actor=_actor(link_owner),
                retrieval_policy=payload.retrieval_policy,
                version_policy=payload.version_policy,
                pinned_version=payload.pinned_version,
                role=payload.role,
                label=payload.label,
                tags=payload.tags,
                access_mode=payload.access_mode,
            )
        except ProjectContextError as e:
            return _refused(e.path, e.message)
        except ProjectError as e:
            # The store refusing the write (too many items, unknown kind) is a
            # rejection of the request, not a server fault.
            return _refused("source", str(e))

        if not result.ok:
            return _refused(_ATTACH_ERROR_FIELD.get(result.error, "source"),
                            result.message)
        return {
            "ok": True,
            "action": result.action,
            "deduplicated": result.deduplicated,
            "message": result.message,
            "link": result.link.to_dict() if result.link else None,
        }

    @router.patch("/{project_id}/context/{link_id}")
    def patch_context(
        project_id: str,
        link_id: str,
        payload: ContextPatchRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Change a link's policy and metadata.

        A version-policy change re-validates the source and recomputes the
        effective revision before it is written, and marks the index stale
        when the revision moved -- stale, not cleared, because the old index
        is still the one serving reads until a new one is complete.
        """
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        patch = payload.model_dump(exclude_none=True)
        if not patch:
            return _refused("patch", "nothing to change")
        refusal = _outside_the_vocabulary(patch)
        if refusal is not None:
            return refusal

        from src.project_context.models import ProjectContextError

        link_owner = _link_owner(request)
        try:
            link = _context_service().update(
                project=project, owner=link_owner, link_id=link_id,
                patch=patch, actor=_actor(link_owner),
            )
        except ProjectContextError as e:
            if e.path == "link_id":
                raise HTTPException(404, "Context link not found")
            return _refused(e.path, e.message)
        except ProjectError as e:
            return _refused("patch", str(e))
        return {"ok": True, "link": link.to_dict()}

    @router.get("/{project_id}/context/{link_id}")
    def inspect_context(
        project_id: str,
        link_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The stored link beside what the source says right now.

        `stale` is the disagreement between the two: the stored revision is a
        claim written when the link was last refreshed, and the resolver is
        the only authority on what the source is today. A link whose source
        has been deleted answers 200 with `ok: false` and stays visible and
        detachable -- a broken link is evidence, and hiding it is how a
        project silently forgets what it was told to know.
        """
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        status = _context_service().inspect(
            project=project, owner=_link_owner(request), link_id=link_id)
        if status.link is None:
            raise HTTPException(404, "Context link not found")
        body: Dict[str, Any] = {"ok": status.state == "ok", **status.to_dict()}
        if status.state != "ok":
            body["error"] = {"path": "source", "message": status.message}
        return body

    @router.post("/{project_id}/context/{link_id}/refresh")
    def refresh_context(
        project_id: str,
        link_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Recompute the revision and say what changed.

        `previous_revision` travels beside `revision` because the index that
        is about to be rebuilt is still serving the old one, and a UI that
        showed only the new number could not tell the user what moved.
        """
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        link_owner = _link_owner(request)
        try:
            result = _context_service().refresh(
                project=project, owner=link_owner, link_id=link_id,
                actor=_actor(link_owner))
        except ProjectError as e:
            return _refused("link", str(e))
        if result.link is None:
            raise HTTPException(404, "Context link not found")
        body: Dict[str, Any] = {
            "ok": result.ok,
            "changed": result.changed,
            "previous_revision": result.previous_revision,
            "revision": result.revision,
            "index_status": result.index_status,
            "state": result.state,
            "message": result.message,
            "link": result.link.to_dict(),
        }
        if not result.ok:
            body["error"] = {"path": "source", "message": result.message}
        return body

    @router.delete("/{project_id}/context/{item_id}")
    def remove_context(
        project_id: str,
        item_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Detach. **The source is never touched.**

        The document, the artifact and the file on disk all survive with their
        versions intact; what is withdrawn is the statement that they belong
        to this project's knowledge. The message says so because the agent
        repeats it to the user.

        One endpoint accepts both a legacy ten-hex ``item_id`` and a
        ``ctx_...`` id because the store normalises both into the same typed
        link. Going through ``ProjectContextService.detach`` is important: it
        emits ``project_context_detached``, which invalidates compiled context
        immediately instead of leaving a removed source cached.
        """
        owner = effective_user(request)
        project = _get_or_404(project_id, owner)
        link_owner = _link_owner(request)
        result = _context_service().detach(
            project=project,
            owner=link_owner,
            link_id=item_id,
            actor=_actor(link_owner),
        )
        if not result.ok:
            raise HTTPException(404, "Context item not found")
        return {
            "success": True,
            "ok": True,
            "link_id": item_id,
            "message": result.message,
        }

    # ------------------------------------------------------------------
    # Resolution — what the frontend asks when the user switches chats
    # ------------------------------------------------------------------

    @router.get("/resolve/session/{session_id}")
    def resolve_for_session(
        session_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Which project (if any) owns this chat, and its workspace.

        The chat path resolves this server-side too and does not trust the
        answer given here — this endpoint exists so the UI can show the right
        workspace pill and project badge, not to decide confinement.
        """
        from services.projects import project_for_session

        project = project_for_session(session_id, effective_user(request))
        if not project:
            return {"project": None}
        return {
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "folder": project.get("folder"),
                "workspace": project.get("workspace"),
                "has_instructions": bool((project.get("instructions") or "").strip()),
                "context_count": len(project.get("context_items") or []),
            }
        }

    # ------------------------------------------------------------------
    # Objectives dashboard (services/objectives.py)
    # ------------------------------------------------------------------

    def _objectives_project(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = _get_or_404(project_id, owner)
        if not (project.get("workspace") or ""):
            raise HTTPException(400, "Project has no folder bound, so it has no objectives")
        return project

    def _apply_or_raise(
        project: Dict[str, Any],
        deltas: List[Dict[str, Any]],
        actor: str,
    ) -> Dict[str, Any]:
        """Apply one dashboard-originated delta, mapping conflicts to HTTP
        errors (a single-delta UI action should fail loudly, unlike the
        agent's batch endpoint which reports conflicts in-band)."""
        from services.objectives import ObjectiveError, apply_deltas
        try:
            result = apply_deltas(project, deltas, actor)
        except ObjectiveError as e:
            raise HTTPException(400, str(e))
        if result["conflicts"]:
            reason = "; ".join(str(c.get("reason") or "conflict") for c in result["conflicts"])
            raise HTTPException(404 if "does not exist" in reason else 400, reason)
        return result

    @router.get("/{project_id}/objectives")
    def list_objectives(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        from services.objectives import dashboard_payload

        def payload() -> Dict[str, Any]:
            project = _objectives_project(project_id, effective_user(request))
            return dashboard_payload(project, log_limit=50)
        if robot.wants(request):
            return robot.reply_sync(request, lambda: lean.objectives(payload()))
        return payload()

    @router.post("/{project_id}/objectives")
    def create_objective(
        project_id: str,
        payload: ObjectiveCreateRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        project = _objectives_project(project_id, effective_user(request))
        delta: Dict[str, Any] = {"op": "ADD", "title": payload.title}
        for key in ("status", "priority", "notes", "deps"):
            value = getattr(payload, key)
            if value is not None:
                delta[key] = value
        result = _apply_or_raise(project, [delta], "user")
        oid = result["applied"][0]["id"]
        created = next(o for o in result["state"]["objectives"] if o["id"] == oid)
        return created

    @router.patch("/{project_id}/objectives/{objective_id}")
    def update_objective(
        project_id: str,
        objective_id: str,
        payload: ObjectiveUpdateRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        project = _objectives_project(project_id, effective_user(request))
        delta: Dict[str, Any] = {"op": "EDIT", "id": objective_id}
        delta.update(payload.model_dump(exclude_none=True))
        if len(delta) == 2:
            raise HTTPException(400, "Nothing to update")
        result = _apply_or_raise(project, [delta], "user")
        updated = next(
            (o for o in result["state"]["objectives"] if o["id"] == objective_id), None
        )
        if not updated:
            raise HTTPException(404, "Objective not found")
        return updated

    @router.delete("/{project_id}/objectives/{objective_id}")
    def drop_objective(
        project_id: str,
        objective_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Drops the objective (status 'dropped'); the record is kept so the
        audit history stays diffable."""
        project = _objectives_project(project_id, effective_user(request))
        _apply_or_raise(
            project,
            [{"op": "KILL", "id": objective_id, "rationale": "removed from dashboard"}],
            "user",
        )
        return {"success": True, "id": objective_id}

    @router.post("/{project_id}/objectives/deltas")
    def apply_objective_deltas(
        project_id: str,
        payload: ObjectiveDeltasRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Batch typed deltas as actor 'agent' (for MCP/dispatch callers).
        Conflicts are reported in-band, never as an HTTP error — a partially
        applicable batch still applies the valid deltas."""
        from services.objectives import ObjectiveError, apply_deltas
        project = _objectives_project(project_id, effective_user(request))
        try:
            return apply_deltas(project, payload.deltas, "agent")
        except ObjectiveError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------
    # Memory files
    # ------------------------------------------------------------------

    @router.get("/{project_id}/memory")
    def list_memory(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = get_store()
        project = _get_or_404(project_id, effective_user(request))
        return {
            "dir": store.memory_dir(project),
            "files": store.list_memory_files(project),
        }

    @router.post("/{project_id}/memory/scaffold")
    def scaffold_memory(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = get_store()
        project = _get_or_404(project_id, effective_user(request))
        try:
            path = store.scaffold_memory(project)
        except OSError as e:
            raise HTTPException(500, f"Could not create the memory folder: {e}")
        if not path:
            raise HTTPException(400, "Project has no folder bound")
        return {"success": True, "index": path}

    @router.get("/{project_id}/memory/{filename}")
    def read_memory(
        project_id: str,
        filename: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = get_store()
        project = _get_or_404(project_id, effective_user(request))
        try:
            return {"name": filename, "content": store.read_memory_file(project, filename)}
        except ProjectError as e:
            raise HTTPException(400, str(e))

    @router.put("/{project_id}/memory/{filename}")
    def write_memory(
        project_id: str,
        filename: str,
        payload: MemoryWriteRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = get_store()
        project = _get_or_404(project_id, effective_user(request))
        try:
            store.write_memory_file(project, filename, payload.content)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        try:
            store.touch(project_id, effective_user(request))
        except OSError as e:
            # The memory file is already saved; activity ordering is useful
            # metadata, but must not turn a successful write into an HTTP 500.
            logger.warning("Could not refresh project activity: %s", e)
        return {"success": True}

    # ------------------------------------------------------------------
    # Preview — what the model actually receives
    # ------------------------------------------------------------------

    @router.get("/{project_id}/preview")
    def preview_system_block(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The exact text prepended to this project's chats. Worth exposing:
        with a small local model, knowing how many characters the project is
        spending of the context window is the difference between 'it works'
        and 'it silently forgot the system prompt'."""
        store = get_store()
        project = _get_or_404(project_id, effective_user(request))
        block = store.system_block(project)
        extra: Dict[str, Any] = {}
        # The per-workspace blocks the agent loop adds on top of the project
        # block (AGENTS.md instructions, repository map) — shown so the user
        # can see the whole budget, not just the project's own text.
        ws = project.get("workspace") or ""
        if ws:
            try:
                from src import project_instructions as _pinstr
                info = _pinstr.read(ws)
                extra["instructions_file"] = {"rel": info.get("rel"), "chars": info.get("chars"), "truncated": info.get("truncated")} if info else None
            except Exception:
                extra["instructions_file"] = None
            try:
                from src import repo_map as _repo_map
                rm = _repo_map.build(ws, "")
                extra["repo_map_chars"] = len(rm)
            except Exception:
                extra["repo_map_chars"] = 0
        return {"block": block, "chars": len(block), **extra}

    # ------------------------------------------------------------------
    # Audit — everything the agent touched in this project
    # ------------------------------------------------------------------

    @router.get("/{project_id}/audit")
    def project_audit(
        project_id: str,
        request: Request,
        limit: int = 200,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Turns that changed files, newest first, each linking to its chat
        and saved message (src/project_audit.py)."""
        project = _get_or_404(project_id, effective_user(request))
        from src import project_audit
        entries = project_audit.load(project_id, limit=max(1, min(int(limit), 2000)))
        # Chats that belonged to the project before it existed / a non-project
        # turn in the same folder are keyed by workspace; merge them in.
        ws = project.get("workspace") or ""
        if ws:
            seen = {(e.get("session_id"), e.get("message_id"), e.get("ts")) for e in entries}
            for e in project_audit.load(project_audit.workspace_key(ws), limit=limit):
                key = (e.get("session_id"), e.get("message_id"), e.get("ts"))
                if key not in seen:
                    entries.append(e)
            entries.sort(key=lambda e: -int(e.get("ts") or 0))
        return {"entries": entries[: max(1, int(limit))], "files": project_audit.files_index(project_id)[:500]}

    @router.delete("/{project_id}/audit")
    def clear_project_audit(
        project_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        _get_or_404(project_id, effective_user(request))
        from src import project_audit
        return {"success": project_audit.clear(project_id)}

    return router
