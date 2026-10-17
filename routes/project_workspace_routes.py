"""UI and MCP surface for real project workspace actions."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_human
from services.projects import get_store, ProjectError
from src.auth_helpers import effective_user, require_user
from src.agent_tools.project_workspace_tools import perform
from src import project_workspace
from src.skill_import_review import SkillReviewError


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    name: str = ""
    key: str = ""
    kind: str = ""
    url: str = ""
    content: str = Field("", max_length=400_000)
    files: dict[str, str] | None = None


class Review(BaseModel):
    key: str
    digest: str
    override: bool = False


def setup_project_workspace_routes():
    router = APIRouter(prefix="/api/projects/{project_id}/workspace", tags=["project-workspace"])

    def project_for(project_id, request):
        owner = effective_user(request)
        project = get_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project, owner

    @router.post("/action")
    def action(project_id: str, body: Action, request: Request, _user=Depends(require_user)):
        project, owner = project_for(project_id, request)
        try:
            return {"ok": True, **perform(project, body.model_dump(exclude_none=True), owner)}
        except (ValueError, OSError, ProjectError, SkillReviewError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/skills/review-activate")
    def review_activate(project_id: str, body: Review, request: Request, _human=Depends(require_human)):
        project, owner = project_for(project_id, request)
        try:
            current = project_workspace.read_skill(project, body.key)
            if current["digest"] != body.digest:
                raise HTTPException(409, "The skill changed since you opened it. Reload and review its current content.")
            return {"ok": True, "skill": project_workspace.activate(project, body.key, reviewed=True, override=body.override, actor=owner, expected_digest=body.digest)}
        except (ValueError, OSError, ProjectError, SkillReviewError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
