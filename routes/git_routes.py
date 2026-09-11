"""Version-control panel API (OBJ-4) -- ``/api/git/*``.

A VS Code-style Source Control view over the git repositories that live under
the owner's linked project folders: discovery (incl. nested repos), status,
log, branches, commit detail/diff, and the mutating operations (checkout,
branch, fetch, pull, push, sync, stage/unstage/discard, commit).

Gating follows the contract (``docs/spec/v2`` OBJ-4) and the pattern
``routes/approvals_routes.py`` already sets: reading is ``require_user``;
anything that changes a repo -- including checkout, since switching branches
under the model's feet is exactly the kind of surprise ``require_human`` gates
elsewhere -- is ``require_human``, so the model's loopback token cannot drive
the panel through its own tool calls.

Every route is owner-scoped through ``git_panel.find_repo_meta`` /
``discover_repos_for_owner``, which only ever walks the CALLING owner's linked
project folders (`services/projects.py`'s own ownership rule). A `repo_id`
that does not come from that walk -- another owner's repo, or an id
constructed from an arbitrary path -- simply never resolves, so it 404s the
same way an unknown id would; there is no separate "is this yours" check to
forget.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_human
from src.auth_helpers import effective_user, require_user
from src import git_panel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class CheckoutBody(BaseModel):
    branch: str = Field(..., min_length=1)
    create: bool = False
    start_point: Optional[str] = None


class CreateBranchBody(BaseModel):
    name: str = Field(..., min_length=1)
    start_point: Optional[str] = None
    checkout: bool = True


class FetchBody(BaseModel):
    remote: Optional[str] = None
    prune: bool = False


class PullBody(BaseModel):
    remote: Optional[str] = None
    branch: Optional[str] = None


class PushBody(BaseModel):
    remote: Optional[str] = None
    branch: Optional[str] = None
    set_upstream: bool = False
    force: bool = False


class PathsBody(BaseModel):
    paths: Optional[List[str]] = None
    all: Optional[bool] = None


class DiscardBody(BaseModel):
    paths: List[str] = Field(default_factory=list)
    confirm: bool = False


class CommitBody(BaseModel):
    message: str = ""
    amend: bool = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    """A flat error body: `error_class` (and, for git failures, `stderr`)
    sit next to `detail` at the TOP level -- not nested under it, which is
    what plain `HTTPException(status, {...})` would produce (FastAPI wraps a
    dict `detail` as `{"detail": {...}}`). `core.middleware`'s OBS-03
    middleware only fills in `error_class` when the body doesn't already
    have one, so a custom class set here is never overwritten.
    """
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


_GIT_MISSING_DETAIL = "git is not installed or not on PATH"


def _git_missing() -> JSONResponse:
    return _error(503, "dependency.missing", _GIT_MISSING_DETAIL)


def _command_failed(exc: git_panel.GitCommandError, *, repo: Optional[Dict[str, Any]] = None) -> JSONResponse:
    snippet = git_panel.stderr_snippet(exc.stderr or exc.stdout or "")
    extra: Dict[str, Any] = {"stderr": snippet}
    if repo is not None:
        extra["repo"] = repo
    return _error(400, "git.command_failed", snippet, **extra)


def _owner(request: Request) -> Optional[str]:
    return effective_user(request)


def _repo_or_404(repo_id: str, owner: Optional[str]) -> Dict[str, Any]:
    meta = git_panel.find_repo_meta(repo_id, owner)
    if meta is None:
        raise HTTPException(404, "Repo not found")
    return meta


def _summary(meta: Dict[str, Any]) -> Dict[str, Any]:
    return git_panel.repo_summary(
        meta["path"], project_id=meta["project_id"], project_name=meta["project_name"],
        root_folder=meta["root_folder"], parent_repo_id=meta["parent_repo_id"],
    )


def _safe_path_or_404(meta: Dict[str, Any], path: str) -> str:
    rel = git_panel.safe_rel_path(meta["path"], path)
    if rel is None:
        raise HTTPException(404, "Path is outside the repository")
    return rel


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def setup_git_routes() -> APIRouter:
    router = APIRouter(prefix="/api/git", tags=["git"])

    # ------------------------------------------------------------------
    # Discovery / repo facts (require_user)
    # ------------------------------------------------------------------
    @router.get("/repos")
    def list_repos(request: Request, project_id: str = "", _u: str = Depends(require_user)) -> Any:
        owner = _owner(request)
        metas = git_panel.discover_repos_for_owner(owner, project_id=project_id)
        if metas is None:
            raise HTTPException(404, "Project not found")
        if not git_panel.git_available():
            return _git_missing()
        return {"repos": [_summary(m) for m in metas], "git_version": git_panel.git_version()}

    @router.get("/repos/{repo_id}")
    def get_repo(repo_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        if not git_panel.git_available():
            return _git_missing()
        return _summary(meta)

    @router.get("/repos/{repo_id}/status")
    def get_status(repo_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        if not git_panel.git_available():
            return _git_missing()
        status = git_panel.repo_status(meta["path"])
        return {k: status[k] for k in (
            "branch", "detached", "ahead", "behind", "upstream",
            "staged", "unstaged", "untracked", "conflicts",
        )}

    @router.get("/repos/{repo_id}/log")
    def get_log(repo_id: str, request: Request, limit: int = 50, cursor: str = "",
                ref: str = "", _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        if not git_panel.git_available():
            return _git_missing()
        return git_panel.log_commits(meta["path"], limit=limit, cursor=cursor or None, ref=ref)

    @router.get("/repos/{repo_id}/branches")
    def get_branches(repo_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        if not git_panel.git_available():
            return _git_missing()
        return git_panel.list_branches(meta["path"])

    @router.get("/repos/{repo_id}/commits/{sha}")
    def get_commit(repo_id: str, sha: str, request: Request, _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        if not git_panel.git_available():
            return _git_missing()
        detail = git_panel.commit_detail(meta["path"], sha)
        if detail is None:
            raise HTTPException(404, "Commit not found")
        return detail

    @router.get("/repos/{repo_id}/commits/{sha}/diff")
    def get_commit_diff(repo_id: str, sha: str, request: Request, path: str = "",
                        _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        rel = _safe_path_or_404(meta, path)
        if not git_panel.git_available():
            return _git_missing()
        return git_panel.commit_file_diff(meta["path"], sha, rel)

    @router.get("/repos/{repo_id}/diff")
    def get_working_diff(repo_id: str, request: Request, path: str = "", staged: int = 0,
                         _u: str = Depends(require_user)) -> Any:
        meta = _repo_or_404(repo_id, _owner(request))
        rel = _safe_path_or_404(meta, path)
        if not git_panel.git_available():
            return _git_missing()
        return git_panel.working_diff(meta["path"], rel, staged=bool(staged))

    # ------------------------------------------------------------------
    # Mutations (require_human)
    # ------------------------------------------------------------------
    @router.post("/repos/{repo_id}/checkout")
    def post_checkout(repo_id: str, body: CheckoutBody, request: Request,
                      _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            branch = git_panel.checkout_branch(
                meta["path"], body.branch, create=body.create, start_point=body.start_point,
            )
        except git_panel.GitDirtyCheckoutError as e:
            return _error(409, "git.dirty", "Local changes would be overwritten by checkout",
                          dirty=e.paths, repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "branch": branch, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/branches")
    def post_create_branch(repo_id: str, body: CreateBranchBody, request: Request,
                           _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            name = git_panel.create_branch(
                meta["path"], body.name, start_point=body.start_point, checkout=body.checkout,
            )
        except git_panel.GitDirtyCheckoutError as e:
            return _error(409, "git.dirty", "Local changes would be overwritten by checkout",
                          dirty=e.paths, repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "branch": name, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/fetch")
    def post_fetch(repo_id: str, body: FetchBody, request: Request,
                   _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            output = git_panel.fetch(meta["path"], remote=body.remote, prune=body.prune)
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/pull")
    def post_pull(repo_id: str, body: PullBody, request: Request,
                  _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            output = git_panel.pull(meta["path"], remote=body.remote, branch=body.branch)
        except git_panel.GitDivergedError as e:
            return _error(409, "git.diverged", "Local branch has diverged from its upstream",
                          ahead=e.ahead, behind=e.behind, repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/push")
    def post_push(repo_id: str, body: PushBody, request: Request,
                  _h: None = Depends(require_human)) -> Any:
        if body.force:
            raise HTTPException(400, "Force push is not supported")
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            output = git_panel.push(
                meta["path"], remote=body.remote, branch=body.branch, set_upstream=body.set_upstream,
            )
        except git_panel.GitRejectedError as e:
            return _error(409, "git.rejected", git_panel.stderr_snippet(e.stderr),
                          stderr=git_panel.stderr_snippet(e.stderr),
                          repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/sync")
    def post_sync(repo_id: str, request: Request, _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            pull_output = git_panel.pull(meta["path"])
        except git_panel.GitDivergedError as e:
            return _error(409, "git.diverged", "Local branch has diverged from its upstream",
                          ahead=e.ahead, behind=e.behind, pull=None, push=None,
                          repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitCommandError as e:
            snippet = git_panel.stderr_snippet(e.stderr or e.stdout or "")
            return _error(400, "git.command_failed", snippet, stderr=snippet, pull=None, push=None,
                          repo=_summary(_repo_or_404(repo_id, owner)))
        pull_result = {"ok": True, "output": pull_output}
        try:
            push_output = git_panel.push(meta["path"])
        except git_panel.GitRejectedError as e:
            snippet = git_panel.stderr_snippet(e.stderr)
            return _error(409, "git.rejected", snippet, stderr=snippet, pull=pull_result, push=None,
                          repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "pull": pull_result, "push": {"ok": True, "output": push_output},
                "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/stage")
    def post_stage(repo_id: str, body: PathsBody, request: Request,
                   _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        rel_paths = None
        if body.paths:
            rel_paths = [_safe_path_or_404(meta, p) for p in body.paths]
        try:
            git_panel.stage(meta["path"], paths=rel_paths, all_=bool(body.all))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/unstage")
    def post_unstage(repo_id: str, body: PathsBody, request: Request,
                     _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        rel_paths = None
        if body.paths:
            rel_paths = [_safe_path_or_404(meta, p) for p in body.paths]
        try:
            git_panel.unstage(meta["path"], paths=rel_paths, all_=bool(body.all))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/discard")
    def post_discard(repo_id: str, body: DiscardBody, request: Request,
                     _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not body.confirm:
            raise HTTPException(400, "Discard requires confirm=true")
        if not body.paths:
            raise HTTPException(400, "No paths to discard")
        if not git_panel.git_available():
            return _git_missing()
        rel_paths = [_safe_path_or_404(meta, p) for p in body.paths]
        try:
            git_panel.discard(meta["path"], rel_paths)
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner))}

    @router.post("/repos/{repo_id}/commit")
    def post_commit(repo_id: str, body: CommitBody, request: Request,
                    _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            result = git_panel.commit(meta["path"], body.message, amend=body.amend)
        except git_panel.GitNothingToCommitError:
            return _error(400, "git.nothing_to_commit", "Empty message or nothing staged",
                          repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitNoIdentityError:
            return _error(409, "git.no_identity", "The repository has no configured user.name/user.email",
                          repo=_summary(_repo_or_404(repo_id, owner)))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner)))
        return {"ok": True, "sha": result["sha"], "short": result["short"], "message": result["message"],
                "repo": _summary(_repo_or_404(repo_id, owner))}

    return router
