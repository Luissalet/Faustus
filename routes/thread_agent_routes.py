"""HTTP layer for the per-thread agent control plane (src/thread_agents.py)."""
from fastapi import APIRouter, HTTPException, Request

from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src import agent_loop_state, thread_agents


def _owner(request: Request, sid: str) -> str:
    _verify_session_owner(request, sid)
    return str(effective_user(request) or "")


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    return body


def _fail(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc.args[0]) if exc.args else "not found")
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc))
    raise exc


def setup_thread_agent_routes() -> APIRouter:
    router = APIRouter(prefix="/api/threads", tags=["thread agents"])

    @router.get("/agents")
    def list_agents():
        return {"agents": thread_agents.agents()}

    @router.get("/agents/commit-author")
    def get_commit_author():
        return {"author": agent_loop_state.configured_git_author()}

    @router.get("/{sid}/agent")
    def get_agent(request: Request, sid: str):
        _owner(request, sid)
        try:
            return {**thread_agents.state(sid), "agents": thread_agents.agents()}
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.get("/{sid}/loop")
    def get_loop(request: Request, sid: str):
        _owner(request, sid)
        loop = agent_loop_state.snapshot(sid)
        return {"session_id": sid, "loop": loop}

    @router.put("/{sid}/agent")
    async def put_agent(request: Request, sid: str):
        _owner(request, sid)
        body = await _body(request)
        try:
            return thread_agents.set_agent(sid, body.get("agent"), model=body.get("model"),
                                           workspace=body.get("workspace"))
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/turn")
    async def post_turn(request: Request, sid: str):
        owner = _owner(request, sid)
        body = await _body(request)
        try:
            return thread_agents.start_turn(sid, body.get("text"), owner=owner or None)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/handoff")
    async def post_handoff(request: Request, sid: str):
        owner = _owner(request, sid)
        body = await _body(request)
        try:
            return thread_agents.start_handoff(sid, body.get("agent"), body.get("task"),
                                               model=body.get("model"), owner=owner or None,
                                               endpoint_id=body.get("endpoint_id"))
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.get("/{sid}/jobs/{job_id}")
    def get_job(request: Request, sid: str, job_id: str):
        _owner(request, sid)
        try:
            return thread_agents.job(sid, job_id)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/jobs/{job_id}/cancel")
    def cancel_job(request: Request, sid: str, job_id: str):
        _owner(request, sid)
        try:
            return thread_agents.cancel(sid, job_id)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/worktree")
    async def create_worktree(request: Request, sid: str):
        _owner(request, sid)
        body = await _body(request)
        try:
            return thread_agents.create_worktree(sid, body.get("repo"), base=body.get("base"),
                                                 branch=body.get("branch"))
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.get("/{sid}/worktree")
    def worktree_status(request: Request, sid: str):
        _owner(request, sid)
        try:
            return thread_agents.worktree_status(sid)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/worktree/commit")
    async def worktree_commit(request: Request, sid: str):
        _owner(request, sid)
        body = await _body(request)
        try:
            return thread_agents.worktree_commit(sid, body.get("message"))
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/worktree/push")
    def worktree_push(request: Request, sid: str):
        _owner(request, sid)
        try:
            return thread_agents.worktree_push(sid)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.post("/{sid}/worktree/pr")
    async def worktree_pr(request: Request, sid: str):
        _owner(request, sid)
        body = await _body(request)
        try:
            return await thread_agents.worktree_pr(sid, title=body.get("title"), body=body.get("body") or "",
                                                   base=body.get("base"), draft=bool(body.get("draft")))
        except (KeyError, ValueError) as exc:
            _fail(exc)

    @router.delete("/{sid}/worktree")
    def remove_worktree(request: Request, sid: str, force: bool = False):
        _owner(request, sid)
        try:
            return thread_agents.remove_worktree(sid, force=force)
        except (KeyError, ValueError) as exc:
            _fail(exc)

    return router
