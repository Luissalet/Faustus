"""F1.4: `src/launch_profiles.py` — validated launch profiles, idempotent
launch, no shell, no silent kill.

Every "process" here is faked: `spawn_detached` is monkeypatched so no real
OS process is ever started (COMUN rule 8), except the one test that asserts
`subprocess.Popen` itself is called with `shell=False` and a literal argv —
there `Popen` is monkeypatched instead, one layer down, so the real spawn
mechanics (`src/process_launch.py`) still run.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import types

import pytest

import routes.connector_routes as connector_routes
import src.launch_profiles as launch_profiles
import src.process_launch as process_launch

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()
    yield
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "app.exe"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _mock_spawn(monkeypatch, *, pid=None, spawned_at=None):
    """Replace `launch_profiles.spawn_detached` (imported by value) with a
    fake that records every call and returns a fixed, always-alive pid — the
    current test process itself, so `_pid_alive` reads real psutil data
    without a real child ever existing."""
    calls = []
    pid = pid if pid is not None else os.getpid()

    def fake_spawn(argv, *, cwd, env, log_path, owner=""):
        calls.append({"argv": argv, "cwd": cwd, "env": env, "log_path": log_path, "owner": owner})
        return process_launch.LaunchResult(pid=pid, log_path=log_path, spawned_at=spawned_at)

    monkeypatch.setattr(launch_profiles, "spawn_detached", fake_spawn)
    return calls


# ── validation ──────────────────────────────────────────────────────────────

def test_relative_executable_is_rejected_on_save():
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="tester", name="X", kind="process", executable="node",
            argv=["server.js"], cwd="/tmp",
        )


async def test_relative_executable_is_rejected_at_launch_time(tmp_path):
    """Saved valid, then the file disappeared — launch-time revalidates."""
    exe = tmp_path / "app.exe"
    exe.write_text("x")
    profile = launch_profiles.create_profile(
        owner="tester", name="X", kind="process", executable=str(exe),
        argv=[], cwd=str(tmp_path),
    )
    exe.unlink()
    result = await launch_profiles.launch(profile["id"])
    assert result["launched"] is False
    assert "error" in result


def test_env_keys_must_match_the_structural_regex():
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="tester", name="X", kind="open_url", url="http://x",
            env={"lowercase-not-allowed": "1"},
        )


def test_argv_with_newline_is_rejected(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="tester", name="X", kind="process", executable=executable,
            argv=["line1\nline2"], cwd=str(tmp_path),
        )


# ── idempotency ─────────────────────────────────────────────────────────────

async def test_already_running_via_readiness_is_not_relaunched(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="tester", name="App", kind="process", executable=executable,
        argv=[], cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:1/health", "timeout_s": 1},
    )
    calls = _mock_spawn(monkeypatch)

    async def ready_now(readiness):
        return True

    monkeypatch.setattr(launch_profiles, "_check_ready_once", ready_now)
    result = await launch_profiles.launch(profile["id"])
    assert result == {"launched": False, "already_running": True}
    # F1.4 forbids launching the APP a second time. It does not forbid
    # opening the desktop shell on the app that is already running -- that
    # is what lot D added ("open the shell instead of returning the URL"),
    # and it is the right answer to "launch" on something already up: show
    # me its window. So the assertion is about this profile's executable,
    # not about the process table being untouched.
    relaunched = [c for c in calls if c["argv"] and c["argv"][0] == executable]
    assert relaunched == [], "the app itself must never be re-launched"


async def test_double_click_concurrent_launch_produces_one_pid(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="tester", name="App", kind="process", executable=executable,
        argv=[], cwd=str(tmp_path),
    )
    calls = _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)

    results = await asyncio.gather(
        launch_profiles.launch(profile["id"]),
        launch_profiles.launch(profile["id"]),
    )
    launched = [r for r in results if r.get("launched")]
    already = [r for r in results if r.get("already_running")]
    assert len(calls) == 1, "a second click must not spawn a second process"
    assert len(launched) == 1
    assert len(already) == 1
    assert already[0]["pid"] == launched[0]["pid"] == os.getpid()


async def test_readiness_timeout_reports_not_ready_and_never_kills(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="tester", name="App", kind="process", executable=executable,
        argv=[], cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:1/health", "timeout_s": 0.2},
    )
    _mock_spawn(monkeypatch, pid=99999999, spawned_at=None)  # an unreachable, never-real pid

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)
    started = time.monotonic()
    result = await launch_profiles.launch(profile["id"])
    elapsed = time.monotonic() - started

    assert result["launched"] is True
    assert result["ready"] is False
    assert "kill" not in json.dumps(result).lower()
    assert elapsed < 5  # bounded by timeout_s, not hanging
    # still recorded as our own launch — nothing was torn down
    assert profile["id"] in launch_profiles._own_launches


async def test_open_url_never_spawns_a_process(monkeypatch, tmp_path):
    profile = launch_profiles.create_profile(
        owner="tester", name="Docs", kind="open_url", url="http://127.0.0.1:5178",
    )
    calls = _mock_spawn(monkeypatch)
    result = await launch_profiles.launch(profile["id"])
    assert result == {"launched": False, "kind": "url", "url": "http://127.0.0.1:5178", "reasons": []}
    assert calls == []


# ── argv passed literally, never through a shell ───────────────────────────

async def test_argv_with_shell_metacharacters_reaches_popen_literally(monkeypatch, tmp_path, executable):
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 424242

    monkeypatch.setattr(process_launch.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(process_launch.process_ownership, "note_started", lambda *a, **k: None)
    monkeypatch.setattr(process_launch.process_ownership, "creation_time", lambda pid: None)

    dangerous = "; rm -rf / #"
    profile = launch_profiles.create_profile(
        owner="tester", name="App", kind="process", executable=executable,
        argv=[dangerous], cwd=str(tmp_path),
    )

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)
    result = await launch_profiles.launch(profile["id"])

    assert result["launched"] is True
    assert captured["argv"] == [executable, dangerous]
    assert captured["kwargs"]["shell"] is False


# ── loopback ────────────────────────────────────────────────────────────────

def test_loopback_url_from_a_remote_client_is_flagged():
    reason = launch_profiles.loopback_reason("http://127.0.0.1:5178/api/health", "203.0.113.5")
    assert reason is not None and "loopback" in reason


def test_loopback_url_from_the_faustus_host_itself_is_not_flagged():
    assert launch_profiles.loopback_reason("http://127.0.0.1:5178/api/health", "127.0.0.1") is None


def test_a_non_loopback_url_is_never_flagged():
    assert launch_profiles.loopback_reason("http://10.0.0.5:5178/api/health", "203.0.113.5") is None


# ── CRUD ─────────────────────────────────────────────────────────────────

def test_crud_roundtrip(tmp_path, executable):
    created = launch_profiles.create_profile(
        owner="tester", name="App", kind="process", executable=executable,
        argv=["--flag"], cwd=str(tmp_path), env={"FOO": "bar"},
    )
    assert launch_profiles.get_profile(created["id"])["name"] == "App"
    updated = launch_profiles.update_profile(created["id"], name="App2")
    assert updated["name"] == "App2"
    assert launch_profiles.delete_profile(created["id"]) is True
    assert launch_profiles.get_profile(created["id"]) is None
    assert launch_profiles.delete_profile(created["id"]) is False


# ── route-level: require_human gates every verb, and a 400 surfaces cleanly ─

async def test_route_level_relative_executable_is_400(monkeypatch, tmp_path):
    from src.mcp_manager import McpManager
    monkeypatch.setattr(connector_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(connector_routes, "require_human", lambda request: None)
    router = connector_routes.setup_connector_routes(McpManager())
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint

    class FakeRequest:
        def __init__(self, body):
            self._body = body
            self.state = types.SimpleNamespace(current_user="tester")
            self.client = None

        async def json(self):
            return self._body

    from fastapi import HTTPException
    # NOT a bare name: on Windows a bare name that `shutil.which` resolves is
    # accepted on purpose (`powershell.exe` has no fixed absolute path across
    # Windows versions). This test used "node", so it passed or failed
    # depending on whether the machine happened to have node on PATH. A
    # relative path with a separator can never take that route.
    relative = os.path.join("bin", "no-such-executable-9f3c2b.exe")
    with pytest.raises(HTTPException) as exc_info:
        await by_path[("POST", "/api/launch-profiles")](
            request=FakeRequest({"name": "X", "kind": "process", "executable": relative,
                                 "argv": [], "cwd": str(tmp_path)}))
    assert exc_info.value.status_code == 400
