"""tests/test_worker_caches_per_worker.py — Darwin #999: every delegated worker
(delegate_agents, an agent definition's minions, dispatch and swarm workers)
gets package caches of its own, listed and cleared over REST and MCP.

Run: python -m pytest tests/test_worker_caches_per_worker.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import worker_caches


@pytest.fixture(autouse=True)
def base(tmp_path, monkeypatch):
    path = tmp_path / "worker_caches"
    monkeypatch.setenv("FAUSTUS_WORKER_CACHES_DIR", str(path))
    monkeypatch.setattr(worker_caches, "_live_worker_ids", lambda: set())
    return path


async def _as_worker(worker_id: str, **meta):
    """What `_run_subagent` does at the start of a worker's task."""
    root = worker_caches.enter_worker(worker_id, **meta)
    env = worker_caches.apply_active({"PATH": "x", "PIP_CACHE_DIR": "/shared/pip"})
    return root, env


def test_each_delegated_worker_gets_its_own_caches(base):
    async def both():
        return await asyncio.gather(
            asyncio.create_task(_as_worker("w1", name="alpha", session_id="w1", parent_session_id="p")),
            asyncio.create_task(_as_worker("w2", name="beta", session_id="w2", parent_session_id="p", agent="tester")),
        )
    (root_a, env_a), (root_b, env_b) = asyncio.run(both())
    assert root_a != root_b
    for name, sub in worker_caches.CACHE_VARS.items():
        assert env_a[name] == os.path.join(root_a, sub)
        assert env_b[name] == os.path.join(root_b, sub)
        assert os.path.isdir(env_a[name])
    assert os.path.dirname(root_a) == str(base)
    # The context dies with the worker's task: the caller is untouched.
    assert worker_caches.active_cache_root() is None
    assert worker_caches.apply_active({"PIP_CACHE_DIR": "/shared/pip"}) == {"PIP_CACHE_DIR": "/shared/pip"}


def test_a_worker_inside_a_fanout_candidate_keeps_the_candidate_caches(tmp_path):
    ws = tmp_path / "exp" / "alt-1"
    ws.mkdir(parents=True)

    async def run():
        with worker_caches.isolated(str(ws)):
            return await asyncio.create_task(_as_worker("w9"))
    root, env = asyncio.run(run())
    assert root == worker_caches.cache_root_for(str(ws))
    assert env["npm_config_cache"] == os.path.join(root, "npm")


def test_listing_shows_both_kinds_with_sizes_and_clear_empties_them(tmp_path):
    async def run():
        root, env = await asyncio.create_task(_as_worker("w1", name="alpha", session_id="w1", parent_session_id="p"))
        with open(os.path.join(env["PIP_CACHE_DIR"], "wheel.whl"), "wb") as fh:
            fh.write(b"x" * 2048)
        return root
    root = asyncio.run(run())
    ws = tmp_path / "exp" / "alt-2"
    ws.mkdir(parents=True)
    worker_caches.isolated_env(str(ws), {})

    listing = worker_caches.list_caches()
    by_kind = {r["kind"]: r for r in listing["workers"]}
    assert set(by_kind) == {"delegate", "fanout"}
    row = by_kind["delegate"]
    assert row["root"] == os.path.abspath(root) and row["session_id"] == "w1" and row["name"] == "alpha"
    assert row["tools"]["pip"] == 2048 and row["bytes"] == 2048
    assert worker_caches.list_caches(parent_session_id="p")["workers"] == [row]

    done = worker_caches.clear(session_id="w1")
    assert done["freed_bytes"] == 2048 and done["cleared"][0]["root"] == row["root"]
    assert worker_caches.list_caches(session_id="w1")["workers"][0]["bytes"] == 0
    # forget drops the folder from the list
    worker_caches.clear(root=row["root"], forget=True)
    assert worker_caches.list_caches(session_id="w1")["workers"] == []


def test_clear_refuses_unknown_folders_and_skips_running_workers(tmp_path, monkeypatch):
    with pytest.raises(worker_caches.WorkerCacheError):
        worker_caches.clear(root=str(tmp_path))
    with pytest.raises(worker_caches.WorkerCacheError):
        worker_caches.clear()
    asyncio.run(_as_worker("live1", session_id="live1"))
    monkeypatch.setattr(worker_caches, "_live_worker_ids", lambda: {"live1"})
    out = worker_caches.clear(all_idle=True)
    assert out["cleared"] == [] and out["skipped"][0]["reason"] == "the worker is running"
    assert worker_caches.clear(all_idle=True, force=True)["cleared"]


def test_native_tool_env_uses_the_worker_caches_not_the_shared_temp():
    from src import tool_execution as te

    async def run():
        root = worker_caches.enter_worker("w5", session_id="w5")
        return root, te._workspace_temp_env({"PATH": "x"}, "sess")
    root, env = asyncio.run(run())
    assert env["TMP"] == os.path.join(root, "tmp")
    assert env["UV_CACHE_DIR"] == os.path.join(root, "uv")


def test_rest_list_and_clear(monkeypatch):
    import routes.worker_caches_routes as routes
    monkeypatch.setattr(routes, "require_user", lambda request: "")
    app = FastAPI()
    app.include_router(routes.setup_worker_caches_routes())
    c = TestClient(app)
    asyncio.run(_as_worker("w7", name="gamma", session_id="w7"))
    body = c.get("/api/agents/worker-caches?session_id=w7").json()
    assert [r["name"] for r in body["workers"]] == ["gamma"]
    assert "PIP_CACHE_DIR" in body["variables"]
    assert c.post("/api/agents/worker-caches/clear", json={"session_id": "w7"}).json()["cleared"]
    assert c.post("/api/agents/worker-caches/clear", json={"root": "C:/nowhere"}).status_code == 400


def test_mcp_tools_render_the_listing(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    names = {t.name for t in ws.TOOLS}
    assert {"workers_caches", "workers_cache_clear"} <= names
    seen = []

    def fake(method, path, body=None, *a, **kw):
        seen.append((method, path, body))
        if method == "GET":
            return {"base": "B", "total_bytes": 2 * 1024 * 1024, "active": 1, "workers": [
                {"name": "alpha", "kind": "delegate", "active": True, "session_id": "w1", "agent": "tester",
                 "bytes": 2 * 1024 * 1024, "tools": {"pip": 2 * 1024 * 1024, "npm": 0}, "root": "B/w1"}]}
        return {"freed_bytes": 1024 * 1024, "cleared": [{"name": "alpha", "root": "B/w1"}], "skipped": []}
    monkeypatch.setattr(ws, "_request", fake)
    text = asyncio.run(ws.call_tool("workers_caches", {"parent_session_id": "p"}))[0].text
    assert "alpha [delegate, running]" in text and "pip 2.0 MB" in text and "agent tester" in text
    assert seen[-1][1] == "/api/agents/worker-caches?parent_session_id=p"
    text = asyncio.run(ws.call_tool("workers_cache_clear", {"session_id": "w1"}))[0].text
    assert "freed 1.0 MB" in text and seen[-1] == ("POST", "/api/agents/worker-caches/clear", {"session_id": "w1"})
