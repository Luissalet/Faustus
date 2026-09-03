"""Stuck-command protection for the agent's bash/python tools.

Seen live (31-08): a delegate_agents worker ran `python -m uvicorn server:app`
in the foreground; the bash tool waited for its 1-hour timeout, the coordinator
hung, and on Stop only the Git-Bash launcher died while bash.exe + uvicorn kept
running. Three layers now: a pre-flight guard for known server launchers, an
idle-output watchdog, and a process-TREE kill on timeout/cancel.
"""
import asyncio
import sys
import time

import pytest

from src.agent_tools import subprocess_tools as st


def test_foreground_server_launch_detection():
    blocked = [
        'cd "D:\\LocalAI\\app" && python -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload',
        "D:/py/python.exe -m uvicorn app:app",
        "npm start", "npm run dev", "yarn dev", "pnpm run serve",
        "node server.js", "flask run --port 5000", "python -m http.server 8080",
        "docker compose up", "tail -f server.log", "watch -n1 ls", "ollama serve",
        "streamlit run app.py", "python manage.py runserver",
    ]
    for c in blocked:
        assert st.foreground_server_launch(c), c
    allowed = [
        "python -m uvicorn server:app --port 8000 &",
        "nohup python -m uvicorn app:app > s.log 2>&1 &",
        "timeout 20 python -m uvicorn server:app",
        "npm run build", "node scripts/build.js", "python -m pytest -q", "pytest tests/ -q && echo ok",
        "docker compose up -d", "tail -n 20 server.log", "ls -la && cat README.md",
        "curl -s http://127.0.0.1:8000/api/stats", "python server.py --check",
        'python -c "import server; print(server.app)"', "git status && git diff --stat", "",
    ]
    for c in allowed:
        assert st.foreground_server_launch(c) is None, c


def test_bash_tool_refuses_foreground_server_before_running():
    res = asyncio.run(st.BashTool().execute("python -m uvicorn server:app --port 8000", {"session_id": "s1"}))
    assert res["exit_code"] == 2
    assert "#!bg" in res["error"] and "timeout 30" in res["error"]
    assert "uvicorn" in res["error"]


def test_local_policy_mentions_foreground_servers():
    from src.agent_harness import local_model_policy
    txt = local_model_policy()
    assert "#!bg" in txt and "uvicorn" in txt and "interactive" in txt


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_idle_watchdog_kills_a_silent_command_and_its_children(monkeypatch):
    """A command that prints once and then sleeps silently is killed after the
    idle budget — including the child process — and reported as `idle`."""
    monkeypatch.setattr(st, "_idle_timeout_seconds", lambda: 1.5)
    monkeypatch.setattr(st.asyncio, "sleep", st.asyncio.sleep)  # no-op, keeps the monkeypatch shape explicit

    async def _go():
        # child `sleep 60` inherits the shell's session → killed by killpg.
        proc = await st._create_bash_subprocess(
            "echo started; sleep 60; echo never",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        t0 = time.time()
        out, err, rc, timed_out = await st._run_subprocess_streaming(proc, timeout=60, idle_timeout=1.5)
        return out, timed_out, time.time() - t0, proc.pid

    out, timed_out, dt, pid = asyncio.run(_go())
    assert timed_out == "idle"
    assert "started" in out and "never" not in out
    assert dt < 20, dt


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_bash_tool_reports_idle_kill(monkeypatch):
    monkeypatch.setattr(st, "_idle_timeout_seconds", lambda: 1.5)
    monkeypatch.setattr(st.shutil, "which", lambda name: None)   # force the plain subprocess path (no tmux)
    res = asyncio.run(st.BashTool().execute("echo hi; sleep 30", {"session_id": "s-idle"}))
    assert res["exit_code"] == 124
    assert "no output for 1s" in res["error"] and "#!bg" in res["error"]
    assert "hi" in res["stdout"]


def test_delegation_args_carry_a_worker_timeout():
    from src.agent_tools.subagent_tools import parse_delegation_args, DEFAULT_WORKER_TIMEOUT_S
    a = parse_delegation_args('{"tasks": ["do x"]}')
    assert a["timeout_s"] == DEFAULT_WORKER_TIMEOUT_S
    a = parse_delegation_args('{"tasks": ["do x"], "timeout_s": 5}')
    assert a["timeout_s"] == 60          # floor
    a = parse_delegation_args('{"tasks": ["do x"], "timeout": 99999}')
    assert a["timeout_s"] == 7200        # cap


def test_worker_timeout_is_reported_and_releases_the_busy_flag(monkeypatch):
    """A worker whose loop never ends is cut at the wall-clock bound: the
    coordinator gets an error+done for it and the sidebar flag is cleared."""
    import src.agent_loop as al
    from src import agent_runs
    from src.agent_tools import subagent_tools as sa
    import src.ai_interaction as ai

    async def hang_forever(*a, **k):
        yield 'data: {"type": "tool_start", "tool": "bash", "command": "python -m uvicorn app:app"}\n\n'
        await asyncio.sleep(3600)

    monkeypatch.setattr(al, "stream_agent_loop", hang_forever)

    class _Parent:
        endpoint_url = "http://x/v1"; model = "m"; headers = None; name = "parent"
    class _SM:
        def get_session(self, sid): return _Parent() if sid == "parent" else None
        def create_session(self, **k): pass
        def save_sessions(self): pass
    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    monkeypatch.setattr(sa, "DEFAULT_WORKER_TIMEOUT_S", 1)
    monkeypatch.setattr(sa, "parse_delegation_args", lambda c, **kw: {"tasks": [{"name": "w", "instruction": "start the server", "model": ""}],
                                                               "parallel": True, "max_rounds": 5, "shared_context": "", "timeout_s": 1})
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: None)
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())

    events = []
    async def cb(p): events.append(p["subagent"])
    agent_runs._EXTERNAL_BUSY.clear()
    res = asyncio.run(sa.DelegateAgentsTool().execute('{"tasks": ["start the server"]}', {"session_id": "parent", "owner": "luis", "progress_cb": cb}))
    assert res["exit_code"] == 1
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started" and "error" in kinds and kinds[-1] == "done"
    done = events[-1]
    assert done["stop_reason"] == "timeout" and "timed out" in (done["error"] or "")
    assert not agent_runs.active_session_ids()
    assert "timed out" in res["output"] or "ERROR" in res["output"].upper()
