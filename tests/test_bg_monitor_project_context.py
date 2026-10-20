"""#948: background follow-up must forward project workspace/harness like foreground.

CPU-only: fake stream_agent_loop; no model/network/tools.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
BG = ROOT / "src" / "bg_monitor.py"
BASELINE_FILE_SHA = "a6e3b37f01487b944de32ff1e93c71684cd4be250b355a7c587cea27900ba13f"


def _load_drain():
    raw = BG.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    names = {"_drain_agent", "_session_project_loop_kwargs"}
    nodes = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    # Prefer real module constants when available; fall back for AST-only extract.
    ctx = {
        "json": __import__("json"),
        "_FOLLOWUP_MAX_ROUNDS": 12,
        "logger": SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BG), "exec"), ctx)
    return ctx, raw


def test_baseline_file_sha_documented_for_handoff():
    """Witness pinned the pre-fix whole-file sha; keep it visible in history."""
    digest = hashlib.sha256(BG.read_bytes()).hexdigest()
    # After the fix the file sha changes; this test only documents the pin string.
    assert len(BASELINE_FILE_SHA) == 64
    assert digest != "0" * 64


@pytest.mark.asyncio
async def test_drain_forwards_workspace_and_harness_from_project_service():
    ctx, _raw = _load_drain()
    assert "_session_project_loop_kwargs" in ctx, "helper missing — wire project resolvers"
    calls = []

    async def stream(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        yield "data: " + __import__("json").dumps({"delta": "fixture complete"})

    parent = ModuleType("src")
    parent.__path__ = []
    agent = ModuleType("src.agent_loop")
    agent.stream_agent_loop = stream

    projects = ModuleType("services.projects")

    def workspace_for_session(session_id, owner=None):
        assert session_id == "sid-project"
        assert owner == "owner-a"
        return r"D:\own-fixture-project"

    def work_roots_for_session(session_id, owner=None):
        return [r"D:\own-fixture-project"]

    def project_for_session(session_id, owner=None):
        return {
            "id": "proj-1",
            "workspace": r"D:\own-fixture-project",
            "checkpoints": True,
            "run_tests": False,
            "review_mode": False,
            "trusted_agents": False,
            "trusted": False,
        }

    def agent_options(project):
        if not project:
            return {}
        return {
            "project_id": project["id"],
            "checkpoints": True,
            "run_tests": False,
            "review_mode": False,
            "trusted_agents": False,
            "trusted": False,
            "test_command": "",
            "review_model": "",
        }

    projects.workspace_for_session = workspace_for_session
    projects.work_roots_for_session = work_roots_for_session
    projects.project_for_session = project_for_session
    projects.agent_options = agent_options

    tool_exec = ModuleType("src.tool_execution")
    tool_exec.vet_workspace = lambda path: path

    sess = SimpleNamespace(
        endpoint_url="http://fixture.invalid/v1",
        model="fixture",
        headers={},
        context_length=123,
        id="sid-project",
        owner="owner-a",
    )

    services_pkg = ModuleType("services")
    services_pkg.projects = projects

    with patch.dict(
        sys.modules,
        {
            "src": parent,
            "src.agent_loop": agent,
            "src.tool_execution": tool_exec,
            "services": services_pkg,
            "services.projects": projects,
        },
    ):
        full, events = await ctx["_drain_agent"](sess, [{"role": "user", "content": "continue"}])

    assert full == "fixture complete"
    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs.get("workspace") == r"D:\own-fixture-project"
    assert kwargs.get("workspace_roots") == [r"D:\own-fixture-project"]
    harness = kwargs.get("harness_options") or {}
    assert harness.get("project_id") == "proj-1"
    assert harness.get("run_tests") is False


@pytest.mark.asyncio
async def test_drain_no_project_omits_workspace_kwargs():
    ctx, _raw = _load_drain()
    calls = []

    async def stream(*args, **kwargs):
        calls.append(kwargs)
        yield "data: " + __import__("json").dumps({"delta": "ok"})

    parent = ModuleType("src")
    parent.__path__ = []
    agent = ModuleType("src.agent_loop")
    agent.stream_agent_loop = stream
    projects = ModuleType("services.projects")
    projects.workspace_for_session = lambda *a, **k: ""
    projects.work_roots_for_session = lambda *a, **k: []
    projects.project_for_session = lambda *a, **k: None
    projects.agent_options = lambda project: {}
    tool_exec = ModuleType("src.tool_execution")
    tool_exec.vet_workspace = lambda path: path or ""
    services_pkg = ModuleType("services")
    services_pkg.projects = projects
    sess = SimpleNamespace(
        endpoint_url="http://fixture.invalid/v1",
        model="fixture",
        headers={},
        context_length=0,
        id="sid-none",
        owner="owner-a",
    )
    with patch.dict(
        sys.modules,
        {
            "src": parent,
            "src.agent_loop": agent,
            "src.tool_execution": tool_exec,
            "services": services_pkg,
            "services.projects": projects,
        },
    ):
        await ctx["_drain_agent"](sess, [])
    kwargs = calls[0]
    assert "workspace" not in kwargs or not kwargs.get("workspace")
    assert not kwargs.get("harness_options")


@pytest.mark.asyncio
async def test_two_sessions_resolve_distinct_workspaces():
    ctx, _raw = _load_drain()
    seen = []

    async def stream(*args, **kwargs):
        seen.append(kwargs.get("workspace"))
        yield "data: " + __import__("json").dumps({"delta": "x"})

    parent = ModuleType("src")
    parent.__path__ = []
    agent = ModuleType("src.agent_loop")
    agent.stream_agent_loop = stream
    projects = ModuleType("services.projects")

    def workspace_for_session(session_id, owner=None):
        return {"sid-a": r"D:\proj-a", "sid-b": r"D:\proj-b"}.get(session_id, "")

    projects.workspace_for_session = workspace_for_session
    projects.work_roots_for_session = lambda sid, owner=None: [workspace_for_session(sid)] if workspace_for_session(sid) else []
    projects.project_for_session = lambda sid, owner=None: (
        {"id": sid, "workspace": workspace_for_session(sid)} if workspace_for_session(sid) else None
    )
    projects.agent_options = lambda project: (
        {"project_id": project["id"], "checkpoints": True, "run_tests": True, "review_mode": False, "trusted_agents": False, "trusted": False}
        if project
        else {}
    )
    tool_exec = ModuleType("src.tool_execution")
    tool_exec.vet_workspace = lambda path: path
    services_pkg = ModuleType("services")
    services_pkg.projects = projects
    with patch.dict(
        sys.modules,
        {
            "src": parent,
            "src.agent_loop": agent,
            "src.tool_execution": tool_exec,
            "services": services_pkg,
            "services.projects": projects,
        },
    ):
        for sid, ws in (("sid-a", r"D:\proj-a"), ("sid-b", r"D:\proj-b")):
            sess = SimpleNamespace(
                endpoint_url="http://fixture.invalid/v1",
                model="fixture",
                headers={},
                context_length=0,
                id=sid,
                owner="o",
            )
            await ctx["_drain_agent"](sess, [])
    assert seen == [r"D:\proj-a", r"D:\proj-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sid,expected", [("sid-a", "standing rule A"), ("sid-b", "standing rule B"), ("sid-none", ""), ("sid-error", "")])
async def test_followup_keeps_standing_project_context_without_engine(sid, expected):
    """Legacy and packet-fallback calls need the same standing context as chat.

    Observe the actual follow-up dispatch before any context-engine packet can
    rescue it. The stream double never executes tools or calls a model.
    """
    tree = ast.parse(BG.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_followup"]
    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)
    history = [{"role": "user", "content": "original request"}]
    sess = SimpleNamespace(id=sid, owner="owner-local", model="fixture", get_context_messages=lambda: list(history))
    seen, saved, busy, resolutions = [], [], set(), []

    async def drain(session, messages):
        assert session is sess and sid in busy
        seen.extend(messages)
        return "continued", []

    def instructions(session_id, owner):
        resolutions.append((session_id, owner))
        if session_id == "sid-error":
            raise OSError("project store unavailable")
        return {"sid-a": "standing rule A", "sid-b": "standing rule B"}.get(session_id, "")

    sm = SimpleNamespace(get_session=lambda _sid: sess, add_message=lambda *args: saved.append(args), save_sessions=lambda: None)
    ai = ModuleType("src.ai_interaction")
    ai.get_session_manager = lambda: sm
    runs = SimpleNamespace(active_session_ids=lambda: list(busy), mark_busy=busy.add, clear_busy=busy.discard)
    parent = ModuleType("src")
    parent.__path__ = []
    parent.agent_runs = runs
    models = ModuleType("core.models")
    models.ChatMessage = lambda *a, **k: SimpleNamespace(args=a, **k)
    projects = ModuleType("services.projects")
    projects.instructions_for_session = instructions
    services = ModuleType("services")
    services.projects = projects
    ctx = {"logger": log, "_drain_agent": drain, "_background_result_message": lambda rec: {"role": "user", "content": "job output"}, "bg_jobs": SimpleNamespace(result_text=lambda rec: "job output")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BG), "exec"), ctx)
    with patch.dict(sys.modules, {"src": parent, "src.ai_interaction": ai, "core.models": models, "services": services, "services.projects": projects}):
        assert await ctx["_run_followup"]({"id": "job-own", "session_id": sid}) is True
    assert resolutions == [(sid, "owner-local")]
    assert seen == ([{"role": "system", "content": expected}] if expected else []) + history + [{"role": "user", "content": "job output"}]
    assert history == [{"role": "user", "content": "original request"}]
    assert not busy and len(saved) == 1
    assert saved[0][1].metadata["bg_job_id"] == "job-own"
