"""Steering a running delegate_agents worker, and the route-level pieces of
the control board:

* `POST /api/chat/subagent/steer/{child_sid}` — owner-verified, 404 when the
  worker is not active, queues the text for the worker's next round.
* `stream_agent_loop(pending_user_messages=...)` — the one hook: the queued
  text becomes a `user` message before the next round and the loop yields a
  `steer` event.
* `GET /api/chat/activity` — adds `workers` from the sub-agent registry.
* `_parse_delegate_tasks` — keeps files/model/reviewer/max_rounds/timeout_s (B3).
"""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from src.agent_tools import subagent_tools as st


# ── route plumbing ─────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body=None, user="alice"):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user=user)
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def routes(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **kw: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )

    def _route(path, method):
        for r in reversed(router.routes):
            if r.path == path and method in getattr(r, "methods", set()):
                return r.endpoint
        raise AssertionError(f"route not found: {method} {path}")
    return _route


@pytest.fixture
async def fake_worker():
    """A live worker registered under child sid 'kid' (task never finishes)."""
    run = st.SubagentRun(0, {"name": "w", "instruction": "x"})
    run.session_id = "kid"
    run.parent_session_id = "parent"

    async def _forever():
        await asyncio.Event().wait()
    task = asyncio.get_running_loop().create_task(_forever())
    st._ACTIVE_WORKERS["kid"] = task
    st._WORKER_RUNS["kid"] = run
    try:
        yield run
    finally:
        task.cancel()
        st._ACTIVE_WORKERS.pop("kid", None)
        st._WORKER_RUNS.pop("kid", None)


@pytest.mark.asyncio
async def test_steer_route_queues_text_for_a_live_worker(routes, fake_worker):
    steer = routes("/api/chat/subagent/steer/{child_session_id}", "POST")
    out = await steer(_Req({"text": "  use pytest, not unittest  "}), "kid")
    assert out == {"ok": True}
    assert st.pending_steers("kid") == [{"text": "use pytest, not unittest", "source": "user"}]
    # drained once handed to the loop
    assert st.pending_steers("kid") == []


@pytest.mark.asyncio
async def test_steer_route_404s_when_the_worker_is_not_active(routes):
    from fastapi import HTTPException
    steer = routes("/api/chat/subagent/steer/{child_session_id}", "POST")
    with pytest.raises(HTTPException) as exc:
        await steer(_Req({"text": "hello"}), "ghost")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_steer_route_rejects_an_empty_text(routes, fake_worker):
    from fastapi import HTTPException
    steer = routes("/api/chat/subagent/steer/{child_session_id}", "POST")
    for body in ({"text": "   "}, {}, None):
        with pytest.raises(HTTPException) as exc:
            await steer(_Req(body), "kid")
        assert exc.value.status_code == 400
    assert st.pending_steers("kid") == []


@pytest.mark.asyncio
async def test_steer_route_verifies_the_owner(monkeypatch, fake_worker):
    from fastapi import HTTPException
    from routes import chat_routes
    calls = []

    def _deny(request, sid, *a, **kw):
        calls.append(sid)
        raise HTTPException(404, "nope")
    monkeypatch.setattr(chat_routes, "_verify_session_owner", _deny)
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    steer = next(r.endpoint for r in reversed(router.routes)
                 if r.path == "/api/chat/subagent/steer/{child_session_id}")
    with pytest.raises(HTTPException):
        await steer(_Req({"text": "hi"}), "kid")
    assert calls == ["kid"] and st.pending_steers("kid") == []


@pytest.mark.asyncio
async def test_activity_lists_the_workers(routes, fake_worker, monkeypatch):
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: ["kid"])
    fake_worker.rounds = 2
    fake_worker.tool_calls = 4
    activity = routes("/api/chat/activity", "GET")
    out = await activity(_Req())
    assert "kid" in out["running"]
    w = out["workers"]["kid"]
    assert w["parent"] == "parent" and w["name"] == "w" and w["role"] == "worker"
    assert w["round"] == 2 and w["tool_calls"] == 4 and w["stalled"] is False
    assert isinstance(w["started_at"], float) and isinstance(w["last_event_at"], float)


@pytest.mark.asyncio
async def test_activity_hides_other_users_workers(monkeypatch, fake_worker):
    from fastapi import HTTPException
    from routes import chat_routes
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [])
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "bob")

    def _deny(request, sid, *a, **kw):
        raise HTTPException(404, "nope")
    monkeypatch.setattr(chat_routes, "_verify_session_owner", _deny)
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    activity = next(r.endpoint for r in reversed(router.routes) if r.path == "/api/chat/activity")
    out = await activity(_Req(user="bob"))
    assert out["workers"] == {}


# ── B3: the /agents form keeps every field the tool understands ───────────

def test_parse_delegate_tasks_keeps_files_model_reviewer_rounds_and_timeout():
    from routes.chat_routes import _parse_delegate_tasks
    out = _parse_delegate_tasks(json.dumps({
        "tasks": [{"name": "a", "instruction": "do a", "files": ["src/a.py", "src/b.py"], "model": "qwen3:8b"},
                  "plain task"],
        "parallel": "false", "reviewer": True, "max_rounds": 9, "timeout_s": 900, "reviewer_model": "big",
    }))
    assert out["parallel"] is False and out["reviewer"] is True
    assert out["max_rounds"] == 9 and out["timeout_s"] == 900 and out["reviewer_model"] == "big"
    assert out["tasks"][0]["files"] == ["src/a.py", "src/b.py"] and out["tasks"][0]["model"] == "qwen3:8b"
    assert "files" not in out["tasks"][1] or out["tasks"][1]["files"] == []
    # optional keys stay absent when not sent (the tool applies its defaults)
    out = _parse_delegate_tasks(json.dumps({"tasks": ["x"]}))
    assert out["parallel"] is True and "reviewer" not in out and "max_rounds" not in out and "timeout_s" not in out


# ── the loop hook ─────────────────────────────────────────────────────────

def test_stream_agent_loop_accepts_pending_user_messages():
    import src.agent_loop as al
    p = inspect.signature(al.stream_agent_loop).parameters["pending_user_messages"]
    assert p.default is None


def test_loop_injects_queued_steer_before_the_next_round(tmp_path, monkeypatch):
    """Round 1 calls a tool; the steer is queued meanwhile; round 2's request
    must carry it as a user message and the loop must announce it."""
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _patch_common, _collect, _events

    _patch_common(monkeypatch, tool_result={"output": "ok", "exit_code": 0})
    seen_requests = []
    queue = []

    async def _exec_and_steer(block, *a, **k):
        # the user steers WHILE round 1's tool runs
        queue.append({"text": "Stop reading, write the test now.", "source": "user"})
        return (block.tool_type, {"output": "x = 1", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _exec_and_steer, raising=False)

    call = '```read_file\n{"path": "a.py"}\n```'

    async def _fake_stream(_candidates, messages, **kwargs):
        seen_requests.append([dict(m) for m in messages])
        if len(seen_requests) == 1:
            yield f'data: {json.dumps({"delta": call})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Done. No files were changed."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    def _pending():
        out, queue[:] = list(queue), []
        return out

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Explain a.py"}],
        max_rounds=4, relevant_tools={"read_file"}, workspace=str(tmp_path),
        pending_user_messages=_pending,
    )
    events = _events(_collect(gen))
    steer = [e for e in events if e.get("type") == "steer"]
    assert len(steer) == 1 and steer[0]["text"] == "Stop reading, write the test now." and steer[0]["source"] == "user"
    assert steer[0]["round"] == 2
    assert len(seen_requests) >= 2
    r1_user = [m for m in seen_requests[0] if m.get("role") == "user" and "write the test now" in str(m.get("content"))]
    r2_user = [m for m in seen_requests[1] if m.get("role") == "user" and "write the test now" in str(m.get("content"))]
    assert not r1_user and len(r2_user) == 1
    assert "Steering" in r2_user[0]["content"] and "user" in r2_user[0]["content"]
    assert seen_requests[1][-1] is not None and "write the test now" in str(seen_requests[1][-1].get("content"))
    assert queue == []
