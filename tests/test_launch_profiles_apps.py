"""F1.4 "Apps" wave (lot A) — `status()`/`stop()`/`restart()`, the new
optional profile fields (`icon`, `open_url`, `stop_cmd`, `description`), the
`launch_state.json` restart survival, and the launch-profile routes these
build on.

Same conventions as `tests/test_launch_profiles.py`: no real OS process is
ever spawned (`spawn_detached` / `subprocess.run` are monkeypatched), and
`launch_profiles.DATA_DIR` is pointed at a disposable `tmp_path` per test.
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
import src.process_center as process_center
import src.process_launch as process_launch

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    launch_profiles._own_launches.clear()
    launch_profiles._own_launches_loaded_for = None
    launch_profiles._profile_locks.clear()
    yield
    launch_profiles._own_launches.clear()
    launch_profiles._own_launches_loaded_for = None
    launch_profiles._profile_locks.clear()


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "app.exe"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _mock_spawn(monkeypatch, *, pid=None, spawned_at=None):
    calls = []
    pid = pid if pid is not None else os.getpid()

    def fake_spawn(argv, *, cwd, env, log_path, owner=""):
        calls.append({"argv": argv, "cwd": cwd, "env": env, "log_path": log_path, "owner": owner})
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("started\n")
        return process_launch.LaunchResult(pid=pid, log_path=log_path, spawned_at=spawned_at)

    monkeypatch.setattr(launch_profiles, "spawn_detached", fake_spawn)
    return calls


async def _never_ready(readiness):
    return False


# ── new optional fields: validation ─────────────────────────────────────────

def test_icon_must_exist_on_disk(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="open_exe", executable=executable,
            icon=str(tmp_path / "missing.png"),
        )


def test_icon_extension_must_be_allowed(tmp_path, executable):
    bad = tmp_path / "icon.txt"
    bad.write_text("not an image")
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="open_exe", executable=executable, icon=str(bad),
        )


def test_icon_too_large_is_rejected(tmp_path, executable, monkeypatch):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG")
    monkeypatch.setattr(launch_profiles.os.path, "getsize", lambda p: launch_profiles.ICON_MAX_BYTES + 1)
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="open_exe", executable=executable, icon=str(icon),
        )


def test_valid_icon_is_accepted(tmp_path, executable):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n")
    profile = launch_profiles.create_profile(
        owner="t", name="X", kind="open_exe", executable=executable, icon=str(icon),
    )
    assert profile["icon"] == str(icon)


def test_open_url_must_be_http(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="open_exe", executable=executable, open_url="not-a-url",
        )


def test_description_length_is_capped(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="open_exe", executable=executable,
            description="x" * (launch_profiles.DESCRIPTION_MAX_CHARS + 1),
        )


def test_stop_cmd_executable_must_be_absolute(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="process", executable=executable, cwd=str(tmp_path),
            stop_cmd={"executable": "stop.sh", "argv": []},
        )


def test_stop_cmd_argv_must_be_string_list(tmp_path, executable):
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="X", kind="process", executable=executable, cwd=str(tmp_path),
            stop_cmd={"executable": executable, "argv": [1, 2]},
        )


def test_valid_stop_cmd_is_accepted(tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="X", kind="process", executable=executable, cwd=str(tmp_path),
        stop_cmd={"executable": executable, "argv": ["--stop"], "cwd": str(tmp_path)},
    )
    assert profile["stop_cmd"]["executable"] == executable


# ── executable: absolute-or-PATH, gated to Windows ──────────────────────────

def test_relative_executable_still_rejected_on_posix(monkeypatch):
    monkeypatch.setattr(launch_profiles, "IS_WINDOWS", False)
    reason = launch_profiles._validate_executable_path("node")
    assert reason is not None and "absolute" in reason


def test_path_resolvable_executable_accepted_on_windows(monkeypatch):
    monkeypatch.setattr(launch_profiles, "IS_WINDOWS", True)
    monkeypatch.setattr(launch_profiles.shutil, "which", lambda name: r"C:\Windows\System32\powershell.exe")
    reason = launch_profiles._validate_executable_path("powershell.exe")
    assert reason is None


def test_path_unresolvable_executable_rejected_on_windows(monkeypatch):
    monkeypatch.setattr(launch_profiles, "IS_WINDOWS", True)
    monkeypatch.setattr(launch_profiles.shutil, "which", lambda name: None)
    reason = launch_profiles._validate_executable_path("nonexistent.exe")
    assert reason is not None


# ── status(): owned / port / none ───────────────────────────────────────────

async def test_status_by_owned_pid(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    await launch_profiles.launch(profile["id"])

    st = await launch_profiles.status(profile["id"])
    assert st["running"] is True
    assert st["source"] == "owned"
    assert st["pid"] == os.getpid()


async def test_status_by_port(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5555/health"},
    )

    async def not_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", not_ready)
    monkeypatch.setattr(
        process_center, "pid_listening_on",
        lambda port, ports_by_pid=None: {"pid": 4242, "created_at": 123.0, "cmdline": "node server.js"}
        if port == 5555 else None,
    )
    st = await launch_profiles.status(profile["id"])
    assert st["running"] is True
    assert st["source"] == "port"
    assert st["pid"] == 4242
    assert st["pid_command"] == "node server.js"


async def test_status_none_when_nothing_matches(tmp_path, executable, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5556/health"},
    )
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    monkeypatch.setattr(process_center, "pid_listening_on", lambda port, ports_by_pid=None: None)
    st = await launch_profiles.status(profile["id"])
    assert st == {"running": False, "source": "none", "pid": None, "created_at": None,
                  "port": 5556, "ready": False, "url": "http://127.0.0.1:5556/health",
                  "pid_command": None, "desktop_open": False}


async def test_status_unknown_profile_never_raises():
    st = await launch_profiles.status("no-such-profile")
    assert st["running"] is False and st["source"] == "none"


async def test_list_statuses_is_one_scan(monkeypatch, tmp_path, executable):
    p1 = launch_profiles.create_profile(
        owner="t", name="A", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5551/health"},
    )
    p2 = launch_profiles.create_profile(
        owner="t", name="B", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5552/health"},
    )
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)

    calls = {"n": 0}
    real_ports_by_pid = process_center._ports_by_pid

    def counting_ports_by_pid():
        calls["n"] += 1
        return {}

    monkeypatch.setattr(process_center, "_ports_by_pid", counting_ports_by_pid)
    statuses = await launch_profiles.list_statuses()
    assert set(statuses) == {p1["id"], p2["id"]}
    assert calls["n"] == 1, "list_statuses() must scan the port table exactly once"
    monkeypatch.setattr(process_center, "_ports_by_pid", real_ports_by_pid)


# ── stop(): stop_cmd / owned / port ─────────────────────────────────────────

async def test_stop_via_stop_cmd_runs_shell_false_and_verifies(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        stop_cmd={"executable": executable, "argv": ["--stop"]},
    )
    captured = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeCompleted()

    monkeypatch.setattr(launch_profiles, "subprocess", types.SimpleNamespace(run=fake_run), raising=False)
    import subprocess as real_subprocess
    monkeypatch.setattr(real_subprocess, "run", fake_run)
    monkeypatch.setattr(launch_profiles, "_status_for", lambda profile, ports_by_pid=None: _ready_false())

    result = await launch_profiles.stop(profile["id"])
    assert result["stopped"] is True
    assert result["how"] == "stop_cmd"
    assert captured["argv"] == [executable, "--stop"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 30


async def _ready_false():
    return {"running": False, "source": "none", "pid": None, "created_at": None,
            "port": None, "ready": False, "url": None, "pid_command": None}


async def test_stop_owned_pid_uses_process_center(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    await launch_profiles.launch(profile["id"])

    calls = []

    def fake_stop(pid, created_at, **kwargs):
        calls.append((pid, created_at))
        return {"ok": True, "code": "", "reason": ""}

    monkeypatch.setattr(process_center, "stop", fake_stop)
    monkeypatch.setattr(launch_profiles, "_status_for", lambda profile, ports_by_pid=None: _ready_false())

    result = await launch_profiles.stop(profile["id"])
    assert result == {"stopped": True, "how": "owned", "reason": ""}
    assert calls and calls[0][0] == os.getpid()
    assert profile["id"] not in launch_profiles._own_launches


async def test_stop_by_port_when_not_owned(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5557/health"},
    )
    monkeypatch.setattr(
        process_center, "pid_listening_on",
        lambda port, ports_by_pid=None: {"pid": 9999, "created_at": 1.0, "cmdline": ""}
        if port == 5557 else None,
    )
    called = {}

    def fake_stop_port(port, **kwargs):
        called["port"] = port
        return {"ok": True, "code": "", "reason": ""}

    monkeypatch.setattr(process_center, "stop_port", fake_stop_port)
    monkeypatch.setattr(launch_profiles, "_status_for", lambda profile, ports_by_pid=None: _ready_false())

    result = await launch_profiles.stop(profile["id"])
    assert result == {"stopped": True, "how": "port", "reason": ""}
    assert called["port"] == 5557


async def test_stop_not_running_when_nothing_found(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    monkeypatch.setattr(process_center, "pid_listening_on", lambda port, ports_by_pid=None: None)
    result = await launch_profiles.stop(profile["id"])
    assert result == {"stopped": False, "how": "not_running"}


# ── restart(): stop then launch, in order ───────────────────────────────────

async def test_restart_stops_then_launches(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    order = []

    async def fake_stop(profile_id, **kwargs):
        order.append("stop")
        return {"stopped": True, "how": "owned", "reason": ""}

    async def fake_launch(profile_id, **kwargs):
        order.append("launch")
        return {"launched": True, "pid": 555, "log_path": "x", "ready": True}

    monkeypatch.setattr(launch_profiles, "stop", fake_stop)
    monkeypatch.setattr(launch_profiles, "launch", fake_launch)
    monkeypatch.setattr(launch_profiles, "_wait_until_not_running", lambda profile, timeout_s: _true())

    result = await launch_profiles.restart(profile["id"])
    assert order == ["stop", "launch"]
    assert result["stopped"] is True
    assert result["launched"] is True
    assert result["pid"] == 555


async def _true():
    return True


# ── icon route: content type + missing/wrong-ext refusal ───────────────────

def test_icon_bytes_none_for_profile_without_icon(tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable,
    )
    assert launch_profiles.icon_bytes(profile["id"]) is None


def test_icon_bytes_content_type_from_extension(tmp_path, executable):
    icon = tmp_path / "icon.svg"
    icon.write_text("<svg></svg>")
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable, icon=str(icon),
    )
    result = launch_profiles.icon_bytes(profile["id"])
    assert result["content_type"] == "image/svg+xml"
    assert result["data"] == b"<svg></svg>"


def test_icon_bytes_refuses_disallowed_extension_even_if_field_bypassed(tmp_path, executable):
    """A profile hand-edited on disk (or created before validation existed)
    must not serve an arbitrary file — the icon route re-checks the
    extension at serve time, not just at save time."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG")
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable, icon=str(icon),
    )
    launch_profiles.update_profile(profile["id"], icon=str(icon))
    # simulate a hand-edited store pointing the icon at something disallowed
    data = launch_profiles._load()
    for p in data["profiles"]:
        if p["id"] == profile["id"]:
            p["icon"] = str(tmp_path / "evil.exe")
    launch_profiles._save(data)
    (tmp_path / "evil.exe").write_bytes(b"MZ")
    assert launch_profiles.icon_bytes(profile["id"]) is None


# ── log tail ─────────────────────────────────────────────────────────────

async def test_log_tail_returns_recent_lines(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    result = await launch_profiles.launch(profile["id"])
    with open(result["log_path"], "a", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(f"line {i}\n")
    tail = launch_profiles.log_tail(profile["id"], lines=2)
    assert tail == "line 3\nline 4\n"


def test_log_tail_none_for_unknown_profile():
    assert launch_profiles.log_tail("no-such-profile") is None


def test_log_tail_empty_string_when_never_launched(tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    assert launch_profiles.log_tail(profile["id"]) == ""


# ── restart survival: data/launch_state.json ────────────────────────────────

async def test_own_launch_survives_a_simulated_restart(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    await launch_profiles.launch(profile["id"])

    state_path = os.path.join(launch_profiles.DATA_DIR, "launch_state.json")
    assert os.path.exists(state_path)
    with open(state_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved[profile["id"]]["pid"] == os.getpid()

    # simulate a fresh Faustus process: forget the in-memory dict entirely
    launch_profiles._own_launches.clear()
    launch_profiles._own_launches_loaded_for = None

    st = await launch_profiles.status(profile["id"])
    assert st["running"] is True
    assert st["source"] == "owned"
    assert st["pid"] == os.getpid()


async def test_dead_pid_is_pruned_on_reload(monkeypatch, tmp_path, executable):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    state_path = os.path.join(str(tmp_path / "data"), "launch_state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump({profile["id"]: {"pid": 999999999, "spawned_at": None, "log_path": "x"}}, fh)
    launch_profiles._own_launches.clear()
    launch_profiles._own_launches_loaded_for = None

    monkeypatch.setattr(launch_profiles, "_check_ready_once", _never_ready)
    monkeypatch.setattr(process_center, "pid_listening_on", lambda port, ports_by_pid=None: None)
    st = await launch_profiles.status(profile["id"])
    assert st["running"] is False
    assert profile["id"] not in launch_profiles._own_launches


# ── routes exist; stop/restart are human-only, status is admin-reachable ────

def test_routes_exist():
    from src.mcp_manager import McpManager
    router = connector_routes.setup_connector_routes(McpManager())
    by_path = {(m, r.path) for r in router.routes for m in getattr(r, "methods", ()) or ()}
    assert ("GET", "/api/launch-profiles/status") in by_path
    assert ("GET", "/api/launch-profiles/{profile_id}/status") in by_path
    assert ("POST", "/api/launch-profiles/{profile_id}/stop") in by_path
    assert ("POST", "/api/launch-profiles/{profile_id}/restart") in by_path
    assert ("GET", "/api/launch-profiles/{profile_id}/icon") in by_path
    assert ("GET", "/api/launch-profiles/{profile_id}/log") in by_path


def test_stop_and_restart_routes_are_human_only(tmp_path, executable):
    from src.mcp_manager import McpManager
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    router = connector_routes.setup_connector_routes(McpManager())
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint

    class Req:
        headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        state = type("S", (), {"current_user": "admin", "is_admin": True})()
        client = type("C", (), {"host": "127.0.0.1"})()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc1:
        asyncio.run(by_path[("POST", "/api/launch-profiles/{profile_id}/stop")](profile["id"], Req()))
    assert exc1.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        asyncio.run(by_path[("POST", "/api/launch-profiles/{profile_id}/restart")](profile["id"], Req()))
    assert exc2.value.status_code == 403


def test_status_route_is_admin_reachable_by_agent_token(tmp_path, executable):
    """Unlike stop/restart, the status reads use `require_admin`, which DOES
    accept the internal agent-tool token — the contract calls these
    "reads" and gates them the same as the rest of this admin-only router."""
    from src.mcp_manager import McpManager
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
    )
    router = connector_routes.setup_connector_routes(McpManager())
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint

    class Req:
        headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        state = type("S", (), {"current_user": "admin", "is_admin": True})()
        client = type("C", (), {"host": "127.0.0.1"})()

    result = asyncio.run(by_path[("GET", "/api/launch-profiles/{profile_id}/status")](profile["id"], Req()))
    assert result["running"] is False
