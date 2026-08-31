"""Projects API — CRUD over projects plus read/write access to their on-disk
memory.

Admin-gated throughout, for the same reason ``workspace_routes`` is: a project
carries a host filesystem path, and the memory endpoints read and write real
files under it. A caller who is not allowed to use the file/shell tools must not
be able to reach those paths through this router either.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src.auth_helpers import effective_user
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


class ContextAddRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=4096)


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
    # Files and folders attached as additional project work roots
    # ------------------------------------------------------------------

    @router.post("/{project_id}/context")
    def add_context(
        project_id: str,
        payload: ContextAddRequest,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _get_or_404(project_id, owner)
        try:
            item = get_store().add_context_item(project_id, payload.path, owner)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"item": item}

    @router.delete("/{project_id}/context/{item_id}")
    def remove_context(
        project_id: str,
        item_id: str,
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _get_or_404(project_id, owner)
        if not get_store().remove_context_item(project_id, item_id, owner):
            raise HTTPException(404, "Context item not found")
        return {"success": True}

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
