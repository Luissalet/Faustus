import json
import os
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
from src import thread_agents as ta


class FakeManager:
    def __init__(self):
        self.sessions = {}

    def get_session(self, sid):
        return self.sessions.setdefault(sid, SimpleNamespace(history=[]))

    def add_message(self, sid, message):
        self.get_session(sid).history.append(message)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    manager = FakeManager()
    monkeypatch.setattr(ta, "_session_manager", lambda: manager)
    calls = []

    def fake_run(agent, prompt, **kw):
        calls.append({"agent": agent, "prompt": prompt, **kw})
        return {"ok": True, "response_text": f"respuesta {len(calls)}", "session_id": f"s-{agent}",
                "seconds": 0.1}
    monkeypatch.setattr(ta, "_runner_call", fake_run)
    with ta._LOCK:
        ta._JOBS.clear()
    return SimpleNamespace(manager=manager, calls=calls, tmp=tmp_path)


def wait(sid, job_id):
    for _ in range(200):
        row = ta.job(sid, job_id)
        if row["status"] != "running":
            return row
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_summary_keeps_newest_and_labels_speakers():
    msgs = [ChatMessage("system", "hidden"), ChatMessage("user", "hola " * 400),
            ChatMessage("assistant", "vale", metadata={"agent_label": "Claude Code"}),
            ChatMessage("assistant", "listo")]
    text = ta.summarize(msgs, per_message=50)
    assert "hidden" not in text
    assert "- Claude Code: vale" in text and "- Faustus: listo" in text
    assert text.splitlines()[0].startswith("- Usuario: hola") and text.splitlines()[0].endswith("…")
    clipped = ta.summarize(msgs, budget=40)
    assert "listo" in clipped and "omitidos" in clipped and "hola" not in clipped


def test_set_agent_validates_and_persists(env):
    with pytest.raises(ValueError):
        ta.set_agent("t1", "nobody")
    out = ta.set_agent("t1", "Claude", model="sonnet")
    assert out["agent"] == "claude" and out["model"] == "sonnet" and out["agent_label"] == "Claude Code"
    assert json.loads((env.tmp / "data" / "thread_agents.json").read_text("utf-8"))["t1"]["agent"] == "claude"
    with pytest.raises(ValueError):
        ta.set_agent("t1", "claude", workspace=str(env.tmp / "missing"))
    assert ta.set_agent("t1", "faustus")["agent"] == "faustus"


def test_turns_go_to_the_runner_and_resume_its_session(env):
    env.manager.add_message("t1", ChatMessage("user", "contexto previo"))
    ta.set_agent("t1", "claude")
    first = wait("t1", ta.start_turn("t1", "primera")["id"])
    assert first["status"] == "done" and first["ok"] is True
    assert env.calls[0]["agent"] == "claude" and env.calls[0]["resume"] is None
    assert "contexto previo" in env.calls[0]["prompt"] and env.calls[0]["prompt"].endswith("primera")
    history = env.manager.get_session("t1").history
    assert [m.role for m in history] == ["user", "user", "assistant"]
    assert history[-1].content == "respuesta 1" and history[-1].metadata["thread_agent"] == "claude"
    # Another agent speaks; the next Claude turn resumes and sees only what is new.
    env.manager.add_message("t1", ChatMessage("assistant", "nota de Faustus"))
    wait("t1", ta.start_turn("t1", "segunda")["id"])
    second = env.calls[1]
    assert second["resume"] == "s-claude"
    assert "nota de Faustus" in second["prompt"] and "contexto previo" not in second["prompt"]


def test_faustus_does_not_run_as_a_runner(env):
    with pytest.raises(ValueError):
        ta.start_turn("t1", "hola")


def test_handoff_returns_result_to_thread_without_changing_agent(env):
    env.manager.add_message("t1", ChatMessage("user", "estamos con el parser"))
    job = wait("t1", ta.start_handoff("t1", "codex", "revisa el parser", model="gpt-x")["id"])
    assert job["kind"] == "handoff" and job["agent"] == "codex"
    call = env.calls[0]
    assert call["model"] == "gpt-x" and call["resume"] is None
    assert "estamos con el parser" in call["prompt"] and "Tarea:\nrevisa el parser" in call["prompt"]
    history = env.manager.get_session("t1").history
    assert history[-2].content == "Pasar a Codex: revisa el parser"
    assert history[-1].metadata["kind"] == "handoff" and history[-1].content == "respuesta 1"
    assert ta.state("t1")["agent"] == "faustus"


def test_failed_run_is_written_as_failure(env, monkeypatch):
    monkeypatch.setattr(ta, "_runner_call", lambda *a, **k: {"ok": False, "error": "not installed"})
    job = wait("t1", ta.start_handoff("t1", "codex", "x")["id"])
    assert job["status"] == "failed" and job["error"] == "not installed"
    assert "no terminó: not installed" in env.manager.get_session("t1").history[-1].content


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("uno\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_worktree_lifecycle_diff_commit_and_runner_cwd(env, repo):
    tree = ta.create_worktree("t1", str(repo))
    assert tree["branch"] == "thread/t1" and tree["base_branch"] == "main"
    wt = tree["path"]
    with pytest.raises(ValueError):
        ta.create_worktree("t1", str(repo))
    with open(os.path.join(wt, "a.txt"), "a", encoding="utf-8") as fh:
        fh.write("dos\n")
    with open(os.path.join(wt, "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("nuevo\n")
    status = ta.worktree_status("t1")
    assert {f["path"] for f in status["files"]} == {"a.txt", "b.txt"}
    assert "+dos" in status["patch"] and "+nuevo" in status["patch"]
    _git(wt, "config", "user.email", "t@example.invalid")
    _git(wt, "config", "user.name", "T")
    ta.worktree_commit("t1", "Add b")
    after = ta.worktree_status("t1")
    assert after["files"] == [] and len(after["commits"]) == 1 and "+nuevo" in after["patch"]
    with pytest.raises(ValueError):
        ta.worktree_push("t1")  # no remote
    ta.set_agent("t1", "claude")
    wait("t1", ta.start_turn("t1", "trabaja")["id"])
    assert env.calls[-1]["workspace"] == wt
    removed = ta.remove_worktree("t1")
    assert removed["branch"] == "thread/t1" and not os.path.isdir(wt)
    assert ta.state("t1")["worktree"] is None
    with pytest.raises(KeyError):
        ta.worktree_status("t1")


def test_job_saved_running_by_a_gone_process_reads_interrupted(env):
    ta._update("t1", lambda row: row.__setitem__("jobs", [{"id": "old", "status": "running"}]))
    assert ta.job("t1", "old")["status"] == "interrupted"
    assert ta.state("t1")["running"] == []


async def test_pull_request_goes_from_thread_branch_to_its_base(env, repo, monkeypatch):
    from src import github_pr
    seen = {}

    async def fake_open(workspace, **kw):
        seen.update(kw, workspace=workspace)
        return {"url": "https://example.invalid/pull/7", "number": 7, "created": True}
    monkeypatch.setattr(github_pr, "open_pull_request", fake_open)
    tree = ta.create_worktree("t1", str(repo))
    out = await ta.worktree_pr("t1", title="Add b", body="cuerpo")
    assert out["number"] == 7 and seen["workspace"] == tree["path"]
    assert seen["head"] == "thread/t1" and seen["base"] == "main" and seen["title"] == "Add b"
    with pytest.raises(ValueError):
        await ta.worktree_pr("t1", title=" ")
    ta.remove_worktree("t1", force=True)


def test_routes(env, repo, monkeypatch):
    from routes import thread_agent_routes as routes
    monkeypatch.setattr(routes, "_verify_session_owner", lambda request, sid: None)
    app = FastAPI()
    app.include_router(routes.setup_thread_agent_routes())
    client = TestClient(app)
    keys = [a["key"] for a in client.get("/api/threads/agents").json()["agents"]]
    assert keys[0] == "faustus" and "claude" in keys and "codex" in keys
    assert client.put("/api/threads/t1/agent", json={"agent": "nobody"}).status_code == 400
    assert client.put("/api/threads/t1/agent", json={"agent": "claude"}).json()["agent"] == "claude"
    job = client.post("/api/threads/t1/turn", json={"text": "hola"}).json()
    assert wait("t1", job["id"])["status"] == "done"
    assert client.get(f"/api/threads/t1/jobs/{job['id']}").json()["ok"] is True
    assert client.get("/api/threads/t1/jobs/nope").status_code == 404
    got = client.get("/api/threads/t1/agent").json()
    assert got["agent"] == "claude" and got["jobs"][-1]["id"] == job["id"] and got["agents"]
    assert client.get("/api/threads/t1/worktree").status_code == 404
    made = client.post("/api/threads/t1/worktree", json={"repo": str(repo)}).json()
    assert made["branch"] == "thread/t1"
    assert client.post("/api/threads/t1/worktree/commit", json={"message": ""}).status_code == 400
    assert client.get("/api/threads/t1/worktree").json()["files"] == []
    assert client.delete("/api/threads/t1/worktree").json()["ok"] is True


async def test_mcp_tools_call_the_thread_api(monkeypatch):
    from mcp_servers import thread_agents_server as srv
    catalog = {t.name: t for t in await srv.list_tools()}
    assert {"thread_agent_set", "thread_send", "thread_handoff", "thread_worktree_create",
            "thread_worktree_status", "thread_worktree_commit", "thread_worktree_push",
            "thread_worktree_pr"} <= set(catalog)
    assert catalog["thread_agent_get"].annotations.readOnlyHint
    assert catalog["thread_handoff"].inputSchema["required"] == ["session_id", "agent", "task"]
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "job1", "status": "running"})
    original = httpx.AsyncClient
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setenv("FAUSTUS_INTERNAL_API_URL", "http://127.0.0.1:63411")
    monkeypatch.setenv("FAUSTUS_INTERNAL_TOOL_TOKEN", "tok")
    out = await srv.call_tool("thread_handoff", {"session_id": "a/b", "agent": "claude", "task": "t"})
    assert json.loads(out[0].text)["id"] == "job1"
    assert seen[0].method == "POST" and seen[0].url.raw_path == b"/api/threads/a%2Fb/handoff"
    assert json.loads(seen[0].content) == {"agent": "claude", "task": "t"}
    assert seen[0].headers["X-Odysseus-Internal-Token"] == "tok"
    await srv.call_tool("thread_worktree_remove", {"session_id": "s", "force": True})
    assert seen[1].method == "DELETE" and seen[1].url.params["force"] == "true"
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "no"})), **kw))
    failed = json.loads((await srv.call_tool("thread_job", {"session_id": "s", "job_id": "j"}))[0].text)
    assert failed["exit_code"] == 1 and failed["status_code"] == 404
