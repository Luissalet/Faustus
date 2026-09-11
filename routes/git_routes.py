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
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_human
from src.auth_helpers import effective_user, require_user
from src import agent_git_policy, git_github, git_identities, git_panel

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


class MergeBody(BaseModel):
    branch: str = Field(..., min_length=1)
    ff: str = Field("auto", pattern="^(auto|only|no)$")
    message: Optional[str] = None
    keep_conflicts: bool = False


class IdentityCreateBody(BaseModel):
    label: str = Field(..., min_length=1)
    ssh_host: Optional[str] = None
    hostname: str = "github.com"
    identity_file: str = Field(..., min_length=1)
    git_user_name: Optional[str] = None
    git_user_email: Optional[str] = None
    write_ssh_config: bool = False


class RepoIdentityBody(BaseModel):
    identity_id: str = Field(..., min_length=1)
    remote: str = "origin"
    set_git_user: bool = True


class CreateRepoGithubBody(BaseModel):
    create: bool = False
    login: Optional[str] = None
    private: bool = True
    description: Optional[str] = None
    push: bool = True
    identity_id: Optional[str] = None


class CreateRepoBody(BaseModel):
    mode: str = Field(..., pattern="^(init|clone)$")
    parent_folder: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    url: Optional[str] = None
    identity_id: Optional[str] = None
    initial_commit: bool = True
    default_branch: str = "main"
    github: Optional[CreateRepoGithubBody] = None


class GithubPublishBody(BaseModel):
    login: str = Field(..., min_length=1)
    private: bool = True
    name: Optional[str] = None
    identity_id: Optional[str] = None
    push: bool = True
    description: Optional[str] = None


class PolicyBody(BaseModel):
    use_branch: Optional[bool] = None
    branch_prefix: Optional[str] = None
    commit: Optional[bool] = None
    commit_message_prefix: Optional[str] = None
    push: Optional[bool] = None
    push_set_upstream: Optional[bool] = None


class RepoPolicyBody(PolicyBody):
    inherit: Optional[bool] = None


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


def _attach_identity_policy(row: Dict[str, Any], owner: Optional[str]) -> Dict[str, Any]:
    """`identity`/`policy` for a full (non-`light`) repo row. Passes the
    `remotes` `repo_summary` already fetched into `active_identity_for_repo`
    so this never runs a second `remote -v` per repo -- alias match only,
    never probes ssh, so this never blocks a repo list on the network
    (that's what the dedicated identities/probe route, and the auto-probe on
    `GET .../identity`, are for). Login is whatever is already cached."""
    ident = git_identities.active_identity_for_repo(row["path"], owner, remotes=row.get("remotes"))
    row["identity"] = (
        {"id": ident["id"], "label": ident["label"], "github_login": ident.get("github_login")}
        if ident else None
    )
    pol = agent_git_policy.effective_policy(owner, row["id"])
    row["policy"] = {"effective": pol["effective"], "overridden": pol["overridden"]}
    return row


def _summary(meta: Dict[str, Any], owner: Optional[str] = None, *, light: bool = False) -> Dict[str, Any]:
    row = git_panel.repo_summary(
        meta["path"], project_id=meta["project_id"], project_name=meta["project_name"],
        root_folder=meta["root_folder"], parent_repo_id=meta["parent_repo_id"],
        projects=meta.get("projects"), light=light,
    )
    if light:
        return row
    return _attach_identity_policy(row, owner)


def _safe_path_or_404(meta: Dict[str, Any], path: str) -> str:
    rel = git_panel.safe_rel_path(meta["path"], path)
    if rel is None:
        raise HTTPException(404, "Path is outside the repository")
    return rel


# ---------------------------------------------------------------------------
# GitHub (Lote 84) -- shared by `POST /repos` (github.create) and
# `POST /repos/{id}/github/publish`.
# ---------------------------------------------------------------------------
def _github_remote_url(login: str, name: str, *, owner: Optional[str], identity_id: Optional[str],
                       created: Dict[str, Any]) -> str:
    """The url `origin` should point at once `login/name` exists on GitHub:
    an explicitly chosen identity's alias/protocol first (an ssh-config
    alias, or the https form for a `gh`-source identity whose account uses
    https), else the matching `gh` account's own protocol, else whatever
    `create_github_repo` itself already resolved (its ssh form)."""
    if identity_id:
        identity = git_identities.find_identity(owner, identity_id)
        if identity:
            if identity.get("ssh_host"):
                return f"git@{identity['ssh_host']}:{login}/{name}.git"
            if identity.get("source") == "gh" and identity.get("protocol") == "https":
                return created.get("https_url") or f"https://github.com/{login}/{name}.git"
    accounts = git_github.gh_accounts().get("accounts", [])
    acct = next((a for a in accounts if a.get("login") == login), None)
    if acct and acct.get("protocol") == "https":
        return created.get("https_url") or f"https://github.com/{login}/{name}.git"
    return created.get("ssh_url") or f"git@github.com:{login}/{name}.git"


def _do_github_create(meta: Dict[str, Any], owner: Optional[str], *, login: str, name: str,
                      private: bool, description: str, identity_id: Optional[str],
                      push: bool) -> Any:
    """Create `login/name` on GitHub, point `origin` at it, and (`push`)
    push the repo's current branch. Returns a `JSONResponse` to return
    AS-IS on failure, or `(created, push_result)` on success -- the caller
    (create-repo or publish) builds its own final response shape around
    that."""
    try:
        created = git_github.create_github_repo(login, name, private=private, description=description)
    except git_github.GhNotAvailableError:
        return _error(503, "dependency.missing", git_github.GH_MISSING_DETAIL, repo=_summary(meta, owner))
    except git_github.GitHubRepoExistsError as e:
        return _error(409, "github.exists", str(e), repo=_summary(meta, owner))
    except git_github.GitHubCommandError as e:
        snippet = git_panel.stderr_snippet(e.stderr or e.stdout or "")
        return _error(502, "github.failed", snippet, stderr=snippet, repo=_summary(meta, owner))

    url = _github_remote_url(login, name, owner=owner, identity_id=identity_id, created=created)
    try:
        git_panel.add_remote(meta["path"], "origin", url)
    except git_panel.GitCommandError as e:
        return _command_failed(e, repo=_summary(meta, owner))

    push_result: Optional[Dict[str, Any]] = None
    if push:
        branch = git_panel.current_branch(meta["path"])[0]
        try:
            output = git_panel.push(meta["path"], remote="origin", branch=branch, set_upstream=True)
            push_result = {"ok": True, "output": output}
        except git_panel.GitRejectedError as e:
            push_result = {"ok": False, "error_class": "git.rejected", "stderr": git_panel.stderr_snippet(e.stderr)}
    return created, push_result


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def setup_git_routes() -> APIRouter:
    router = APIRouter(prefix="/api/git", tags=["git"])

    # ------------------------------------------------------------------
    # Discovery / repo facts (require_user)
    # ------------------------------------------------------------------
    @router.get("/repos")
    def list_repos(request: Request, project_id: str = "", light: int = 0,
                   _u: str = Depends(require_user)) -> Any:
        owner = _owner(request)
        metas = git_panel.discover_repos_for_owner(owner, project_id=project_id)
        if metas is None:
            raise HTTPException(404, "Project not found")
        if not git_panel.git_available():
            return _git_missing()
        # Each repo's summary is its own `git` subprocess round trip;
        # `repo_summaries` overlaps them across a small thread pool instead
        # of paying every process-spawn serially (most of the 9.5s -> <2s
        # budget for 24 repos on Windows), and `?light=1` -- the polling
        # adapter's shape -- skips remotes/user/identity/policy entirely.
        is_light = bool(light)
        rows = git_panel.repo_summaries(metas, light=is_light)
        if not is_light:
            rows = [_attach_identity_policy(row, owner) for row in rows]
        return {"repos": rows, "git_version": git_panel.git_version()}

    @router.get("/repos/{repo_id}")
    def get_repo(repo_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        return _summary(meta, owner)

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
                          dirty=e.paths, repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "branch": branch, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
                          dirty=e.paths, repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "branch": name, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
                          ahead=e.ahead, behind=e.behind, repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "output": output, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            snippet = git_panel.stderr_snippet(e.stderr or e.stdout or "")
            return _error(400, "git.command_failed", snippet, stderr=snippet, pull=None, push=None,
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        pull_result = {"ok": True, "output": pull_output}
        try:
            push_output = git_panel.push(meta["path"])
        except git_panel.GitRejectedError as e:
            snippet = git_panel.stderr_snippet(e.stderr)
            return _error(409, "git.rejected", snippet, stderr=snippet, pull=pull_result, push=None,
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "pull": pull_result, "push": {"ok": True, "output": push_output},
                "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

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
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

    @router.post("/repos/{repo_id}/commit")
    def post_commit(repo_id: str, body: CommitBody, request: Request,
                    _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        project_id = meta.get("project_id")
        if not project_id:
            projects = meta.get("projects") or []
            project_id = projects[0]["id"] if projects else None
        try:
            result = git_panel.commit(meta["path"], body.message, amend=body.amend, project_id=project_id)
        except git_panel.GitNothingToCommitError:
            return _error(400, "git.nothing_to_commit", "Empty message or nothing staged",
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitNoIdentityError:
            return _error(409, "git.no_identity", "The repository has no configured user.name/user.email",
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "sha": result["sha"], "short": result["short"], "message": result["message"],
                "repo": _summary(_repo_or_404(repo_id, owner), owner)}

    @router.post("/repos/{repo_id}/merge")
    def post_merge(repo_id: str, body: MergeBody, request: Request,
                   _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            result = git_panel.merge(
                meta["path"], body.branch, ff=body.ff, message=body.message,
                keep_conflicts=body.keep_conflicts,
            )
        except git_panel.GitDirtyCheckoutError as e:
            return _error(409, "git.dirty", "Local changes would be overwritten by merge",
                          dirty=e.paths, repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitMergeConflictError as e:
            detail = (
                "The merge produced conflicts and was aborted automatically; nothing changed."
                if e.aborted else
                "The merge is paused with unresolved conflicts; resolve them in your editor and "
                "commit, or call merge/abort."
            )
            return _error(409, "git.merge_conflict", detail, conflicts=e.conflicts, aborted=e.aborted,
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "sha": result["sha"], "fast_forward": result["fast_forward"],
                "conflicts": result["conflicts"], "repo": _summary(_repo_or_404(repo_id, owner), owner)}

    @router.post("/repos/{repo_id}/merge/abort")
    def post_merge_abort(repo_id: str, request: Request, _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            git_panel.merge_abort(meta["path"])
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        return {"ok": True, "repo": _summary(_repo_or_404(repo_id, owner), owner)}

    @router.delete("/repos/{repo_id}/branches/{name:path}")
    def delete_branch_route(repo_id: str, name: str, request: Request, force: int = 0, remote: int = 0,
                            _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        try:
            git_panel.delete_branch(meta["path"], name, force=bool(force))
        except git_panel.GitBranchIsCurrentError:
            return _error(409, "git.branch_is_current", "Cannot delete the currently checked out branch",
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitBranchUnmergedError:
            return _error(409, "git.branch_unmerged",
                          f"Branch {name!r} is not fully merged; pass force=1 to delete it anyway",
                          repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        resp: Dict[str, Any] = {"ok": True, "deleted": name, "repo": _summary(_repo_or_404(repo_id, owner), owner)}
        if remote:
            try:
                git_panel.delete_remote_branch(meta["path"], "origin", name)
                resp["remote_deleted"] = True
            except git_panel.GitCommandError as e:
                resp["remote_deleted"] = False
                resp["remote_error"] = git_panel.stderr_snippet(e.stderr or e.stdout)
        return resp

    # ------------------------------------------------------------------
    # SSH identities (Lote 82) -- read is require_user, everything that
    # writes a file (a manual identity, an ~/.ssh/config block) or runs a
    # network probe is require_human, same split as every mutation above.
    # ------------------------------------------------------------------
    @router.get("/identities")
    def get_identities(request: Request, _u: str = Depends(require_user)) -> Any:
        return git_identities.list_identities(_owner(request))

    @router.post("/identities")
    def post_identity(body: IdentityCreateBody, request: Request,
                      _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        try:
            record = git_identities.create_manual_identity(
                owner, label=body.label, ssh_host=body.ssh_host, hostname=body.hostname,
                identity_file=body.identity_file, git_user_name=body.git_user_name,
                git_user_email=body.git_user_email, write_ssh_config=body.write_ssh_config,
            )
        except git_identities.GitIdentityError as e:
            return _error(400, f"git.{e.code}", e.detail)
        return JSONResponse(status_code=201, content={"identity": record})

    @router.delete("/identities/{identity_id}")
    def delete_identity(identity_id: str, request: Request,
                        _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        match = git_identities.find_identity(owner, identity_id)
        if match is not None and match.get("source") != "manual":
            return _error(400, "git.identity_not_manual", "Only manually added identities can be deleted")
        if not git_identities.delete_manual_identity(owner, identity_id):
            raise HTTPException(404, "Identity not found")
        return {"ok": True}

    @router.post("/identities/{identity_id}/probe")
    def post_probe_identity(identity_id: str, request: Request,
                            _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        identity = git_identities.find_identity(owner, identity_id)
        if identity is None:
            raise HTTPException(404, "Identity not found")
        return git_identities.probe_identity(identity, force=True)

    @router.get("/repos/{repo_id}/identity")
    def get_repo_identity(repo_id: str, request: Request, remote: str = "origin",
                          _u: str = Depends(require_user)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        return git_identities.repo_identity_info(meta["path"], owner, remote=remote)

    @router.put("/repos/{repo_id}/identity")
    def put_repo_identity(repo_id: str, body: RepoIdentityBody, request: Request,
                          _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        identity = git_identities.find_identity(owner, body.identity_id)
        if identity is None:
            raise HTTPException(404, "Identity not found")
        if not git_panel.git_available():
            return _git_missing()
        try:
            result = git_identities.set_repo_identity(
                meta["path"], identity, remote=body.remote, set_git_user=body.set_git_user,
            )
        except git_identities.GitIdentityError as e:
            return _error(400, f"git.{e.code}", e.detail, repo=_summary(_repo_or_404(repo_id, owner), owner))
        except git_panel.GitCommandError as e:
            return _command_failed(e, repo=_summary(_repo_or_404(repo_id, owner), owner))
        result["repo"] = _summary(_repo_or_404(repo_id, owner), owner)
        return result

    # ------------------------------------------------------------------
    # Create / clone repos (Lote 82)
    # ------------------------------------------------------------------
    @router.get("/folders")
    def get_folders(request: Request, _u: str = Depends(require_user)) -> Any:
        return {"folders": git_panel.list_owner_folders(_owner(request))}

    @router.post("/repos")
    def post_create_repo(body: CreateRepoBody, request: Request,
                         _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        parent_real = git_panel.resolve_allowed_parent_folder(owner, body.parent_folder)
        if parent_real is None:
            return _error(403, "git.folder_not_allowed", "parent_folder is not one of your linked folders")
        if not git_panel.git_available():
            return _git_missing()
        try:
            path = git_panel.create_repo(
                parent_real, body.name, mode=body.mode, owner=owner, url=body.url,
                identity_id=body.identity_id, initial_commit=body.initial_commit,
                default_branch=body.default_branch or "main",
            )
        except git_panel.GitInvalidNameError as e:
            return _error(400, "git.invalid_name", str(e) or "invalid repository name/mode")
        except git_panel.GitRepoExistsError as e:
            return _error(409, "git.exists", f"{e.path} already exists and is not empty")
        except git_panel.GitCloneFailedError as e:
            snippet = git_panel.stderr_snippet(e.stderr)
            return _error(409, "git.clone_failed", snippet, stderr=snippet)
        except git_panel.GitNoIdentityError:
            return _error(409, "git.no_identity",
                          "No git user.name/user.email configured to create the initial commit with")
        except git_panel.GitCommandError as e:
            return _command_failed(e)

        repo_id = git_panel.compute_repo_id(path)
        # Built directly rather than re-walking discovery (`_repo_or_404`):
        # the new repo might sit past this project's depth/MAX_REPOS
        # discovery caps even though it was created inside a linked folder.
        root_folder, project_id, project_name = parent_real, None, None
        for folder in git_panel.list_owner_folders(owner):
            folder_key = os.path.normcase(folder["path"])
            if os.path.normcase(path) == folder_key or path.startswith(folder["path"] + os.sep):
                root_folder, project_id, project_name = folder["path"], folder["project_id"], folder["project_name"]
                break
        meta = {
            "id": repo_id, "path": path, "name": os.path.basename(path.rstrip(os.sep)) or path,
            "project_id": project_id, "project_name": project_name,
            "root_folder": root_folder, "parent_repo_id": None,
        }

        resp_body: Dict[str, Any] = {"repo": _summary(meta, owner), "github": None, "push": None}
        # "clone no aplica" (contract): a cloned repo already has whatever
        # `origin` its source pointed at, so `github.create` is a no-op for
        # `mode: "clone"` even if the body asked for it.
        if body.mode == "init" and body.github and body.github.create:
            login = (body.github.login or "").strip()
            if not login:
                return _error(400, "github.login_required", "github.login is required to create on GitHub")
            result = _do_github_create(
                meta, owner, login=login, name=body.name, private=body.github.private,
                description=body.github.description or "", identity_id=body.github.identity_id,
                push=body.github.push,
            )
            if isinstance(result, JSONResponse):
                return result
            created, push_result = result
            resp_body = {"repo": _summary(meta, owner), "github": created, "push": push_result}
        return JSONResponse(status_code=201, content=resp_body)

    @router.post("/repos/{repo_id}/github/publish")
    def post_github_publish(repo_id: str, body: GithubPublishBody, request: Request,
                            _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        meta = _repo_or_404(repo_id, owner)
        if not git_panel.git_available():
            return _git_missing()
        existing = next((r for r in git_panel.repo_remotes(meta["path"]) if r.get("name") == "origin"), None)
        if existing:
            return _error(409, "git.remote_exists", "This repository already has an 'origin' remote",
                          repo=_summary(meta, owner))
        name = (body.name or "").strip() or meta["name"]
        result = _do_github_create(
            meta, owner, login=body.login, name=name, private=body.private,
            description=body.description or "", identity_id=body.identity_id, push=body.push,
        )
        if isinstance(result, JSONResponse):
            return result
        created, push_result = result
        return JSONResponse(status_code=201, content={
            "repo": _summary(meta, owner), "github": created, "push": push_result,
        })

    @router.get("/github/accounts")
    def get_github_accounts(_u: str = Depends(require_user)) -> Any:
        return git_github.gh_accounts()

    # ------------------------------------------------------------------
    # Agent git policy (Lote 82)
    # ------------------------------------------------------------------
    @router.get("/policy")
    def get_policy(_u: str = Depends(require_user)) -> Any:
        return {"policy": agent_git_policy.get_global_policy()}

    @router.put("/policy")
    def put_policy(body: PolicyBody, _h: None = Depends(require_human)) -> Any:
        patch = body.model_dump(exclude_unset=True)
        try:
            merged = agent_git_policy.set_global_policy(patch)
        except agent_git_policy.GitPolicyError as e:
            return _error(400, f"git.{e.code}", e.detail)
        return {"policy": merged}

    @router.get("/repos/{repo_id}/policy")
    def get_repo_policy(repo_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = _owner(request)
        _repo_or_404(repo_id, owner)
        return {"policy": agent_git_policy.effective_policy(owner, repo_id)}

    @router.put("/repos/{repo_id}/policy")
    def put_repo_policy(repo_id: str, body: RepoPolicyBody, request: Request,
                        _h: None = Depends(require_human)) -> Any:
        owner = _owner(request)
        _repo_or_404(repo_id, owner)
        patch = body.model_dump(exclude_unset=True)
        try:
            agent_git_policy.set_repo_override(owner, repo_id, patch)
        except agent_git_policy.GitPolicyError as e:
            return _error(400, f"git.{e.code}", e.detail)
        return {"policy": agent_git_policy.effective_policy(owner, repo_id)}

    return router
