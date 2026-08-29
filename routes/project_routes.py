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


class MemoryWriteRequest(BaseModel):
    content: str = Field("", max_length=MAX_MEMORY_FILE)


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
        return {"block": block, "chars": len(block)}

    return router
