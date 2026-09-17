"""F1.4 "Apps" wave (lot D) — `src.launch_profiles.open_desktop()` and its
wiring into `launch()`/`stop()`/`status()`.

Same conventions as `tests/test_launch_profiles_apps.py`: no real OS process
is ever spawned (`spawn_detached` is monkeypatched), and
`launch_profiles.DATA_DIR` is pointed at a disposable `tmp_path` per test.
Electron itself is never installed here — these tests only cover the Python
side deciding WHETHER and HOW to spawn `desktop/app-shell.cjs`, never the
shell itself (that is `desktop/app-shell.test.cjs`, run with `node --test`).
"""
from __future__ import annotations

import os
import stat

import pytest

import src.launch_profiles as launch_profiles
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


# ── desktop default field ───────────────────────────────────────────────────

def test_desktop_defaults_true_with_an_open_url(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5555/", open_url="http://127.0.0.1:5555/",
    )
    assert profile["desktop"] is True


def test_desktop_defaults_true_with_an_http_readiness_url(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5556/health"},
    )
    assert profile["desktop"] is True


def test_desktop_defaults_false_with_neither(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable,
    )
    assert profile["desktop"] is False


def test_desktop_explicit_false_overrides_the_default(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5557/health"}, desktop=False,
    )
    assert profile["desktop"] is False


def test_desktop_must_be_a_boolean():
    with pytest.raises(launch_profiles.ProfileValidationError):
        launch_profiles.create_profile(
            owner="t", name="App", kind="open_exe", executable="", desktop="yes",
        )


# ── open_desktop(): missing runtime ─────────────────────────────────────────

async def test_open_desktop_missing_runtime_reports_install_hint(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5558/", open_url="http://127.0.0.1:5558/",
    )
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: None)
    result = launch_profiles.open_desktop(profile["id"])
    assert result == {"opened": False, "error": "desktop runtime not installed (desktop/: npm install)"}


async def test_open_desktop_no_target_url_configured(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable, desktop=True,
    )
    result = launch_profiles.open_desktop(profile["id"])
    assert result == {"opened": False, "error": "no open_url or readiness url configured for a desktop window"}


async def test_open_desktop_unknown_profile():
    result = launch_profiles.open_desktop("no-such-profile")
    assert result == {"opened": False, "error": "no such launch profile: no-such-profile"}


# ── open_desktop(): argv built correctly ────────────────────────────────────

async def test_open_desktop_spawns_the_shell_with_expected_argv(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="Sculptor's Hoard", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:8767/ui", open_url="http://127.0.0.1:8767/ui",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=4242, spawned_at=123.0)

    result = launch_profiles.open_desktop(profile["id"])

    assert result == {"opened": True, "pid": 4242, "log_path": calls[0]["log_path"]}
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == fake_binary
    assert argv[1] == os.path.join(launch_profiles.BASE_DIR, "desktop", "app-shell.cjs")
    assert "--url=http://127.0.0.1:8767/ui" in argv
    assert f"--title={profile['name']}" in argv
    assert f"--slug={profile['id']}" in argv
    # No icon configured -> no --icon flag at all.
    assert not any(a.startswith("--icon=") for a in argv)
    assert calls[0]["cwd"] == os.path.join(launch_profiles.BASE_DIR, "desktop")
    assert calls[0]["owner"] == f"desktop_shell:{profile['id']}"
    # ELECTRON_RUN_AS_NODE must never leak into the shell's environment.
    assert "ELECTRON_RUN_AS_NODE" not in calls[0]["env"]


async def test_open_desktop_includes_icon_flag_when_configured(executable, tmp_path, monkeypatch):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n")
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5559/", open_url="http://127.0.0.1:5559/", icon=str(icon),
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=1, spawned_at=1.0)

    launch_profiles.open_desktop(profile["id"])
    assert f"--icon={icon}" in calls[0]["argv"]


async def test_open_desktop_prefers_readiness_origin_over_full_path(executable, tmp_path, monkeypatch):
    """The window should point at the ORIGIN of a readiness URL, not the
    health-check path itself — `/api/health` is not a page a human wants to
    look at."""
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:8767/api/health"},
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=1, spawned_at=1.0)

    launch_profiles.open_desktop(profile["id"])
    assert "--url=http://127.0.0.1:8767" in calls[0]["argv"]


# ── open_desktop(): idempotent, no double open ──────────────────────────────

async def test_open_desktop_does_not_open_a_second_window(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5560/", open_url="http://127.0.0.1:5560/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)

    first = launch_profiles.open_desktop(profile["id"])
    assert first["opened"] is True
    assert len(calls) == 1

    second = launch_profiles.open_desktop(profile["id"])
    assert second == {"opened": False, "already_open": True, "pid": os.getpid()}
    assert len(calls) == 1, "a second call must not spawn a second shell window"


async def test_open_desktop_reopens_once_the_previous_shell_is_gone(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5561/", open_url="http://127.0.0.1:5561/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    # A pid that will never be alive -> as good as "the shell process exited".
    calls = _mock_spawn(monkeypatch, pid=999999999, spawned_at=None)

    first = launch_profiles.open_desktop(profile["id"])
    assert first["opened"] is True
    second = launch_profiles.open_desktop(profile["id"])
    assert second["opened"] is True, "the previous shell pid is dead, so this must open a new one"
    assert len(calls) == 2


# ── status(): desktop_open ───────────────────────────────────────────────────

async def test_status_desktop_open_reflects_a_live_shell_pid(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5562/", open_url="http://127.0.0.1:5562/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)

    st_before = await launch_profiles.status(profile["id"])
    assert st_before["desktop_open"] is False

    launch_profiles.open_desktop(profile["id"])
    st_after = await launch_profiles.status(profile["id"])
    assert st_after["desktop_open"] is True


async def test_status_desktop_open_false_for_a_dead_shell_pid(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5563/", open_url="http://127.0.0.1:5563/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    _mock_spawn(monkeypatch, pid=999999999, spawned_at=None)

    launch_profiles.open_desktop(profile["id"])
    st = await launch_profiles.status(profile["id"])
    assert st["desktop_open"] is False


# ── launch(): opens the shell as a side effect, response shape unchanged ────

async def test_launch_open_url_kind_still_returns_the_same_shape(executable, tmp_path, monkeypatch):
    """Lot D: launch() opens the desktop shell for a `desktop: true` profile
    as a SIDE EFFECT — the returned dict keeps exactly the shape it had
    before lot D ("keep returning {kind, url} fields for compatibility")."""
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5564/", open_url="http://127.0.0.1:5564/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)

    result = await launch_profiles.launch(profile["id"])
    assert result == {"launched": False, "kind": "url", "url": "http://127.0.0.1:5564/", "reasons": []}
    assert len(calls) == 1, "the desktop shell must have been spawned as a side effect"


async def test_launch_does_not_open_desktop_when_disabled(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5565/", open_url="http://127.0.0.1:5565/", desktop=False,
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    calls = _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)

    await launch_profiles.launch(profile["id"])
    assert calls == []


async def test_launch_process_kind_opens_desktop_without_deadlocking(executable, tmp_path, monkeypatch):
    """A regression guard: `open_desktop()` takes the SAME per-profile lock
    `launch()` already holds while deciding what to do, so every call to it
    from inside `launch()` must happen only after that lock is released."""
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="process", executable=executable, cwd=str(tmp_path),
        readiness={"url": "http://127.0.0.1:5566/health", "timeout_s": 0.2},
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)
    spawn_calls = []
    pids = iter([111, 222])

    def fake_spawn(argv, *, cwd, env, log_path, owner=""):
        spawn_calls.append(argv)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("x\n")
        return process_launch.LaunchResult(pid=next(pids), log_path=log_path, spawned_at=None)

    monkeypatch.setattr(launch_profiles, "spawn_detached", fake_spawn)

    import asyncio
    result = await asyncio.wait_for(launch_profiles.launch(profile["id"]), timeout=5)
    assert result["launched"] is True
    assert result["ready"] is False
    # one spawn for the app itself, one for the desktop shell
    assert len(spawn_calls) == 2


# ── stop(): also terminates the shell pid ───────────────────────────────────

async def test_stop_also_terminates_the_desktop_shell_pid(executable, tmp_path, monkeypatch):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_url", executable="", cwd="",
        url="http://127.0.0.1:5567/", open_url="http://127.0.0.1:5567/",
    )
    fake_binary = str(tmp_path / "electron")
    monkeypatch.setattr(launch_profiles, "_electron_binary", lambda: fake_binary)
    _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    launch_profiles.open_desktop(profile["id"])
    assert (await launch_profiles.status(profile["id"]))["desktop_open"] is True

    stopped_pids = []

    from src import process_center
    def fake_stop(pid, created_at, **kwargs):
        stopped_pids.append(pid)
        return {"ok": True, "code": "", "reason": ""}
    monkeypatch.setattr(process_center, "stop", fake_stop)
    monkeypatch.setattr(process_center, "pid_listening_on", lambda port, ports_by_pid=None: None)

    await launch_profiles.stop(profile["id"])
    assert os.getpid() in stopped_pids
    st = await launch_profiles.status(profile["id"])
    assert st["desktop_open"] is False


async def test_stop_desktop_never_raises_when_nothing_was_open(executable, tmp_path):
    profile = launch_profiles.create_profile(
        owner="t", name="App", kind="open_exe", executable=executable,
    )
    launch_profiles._stop_desktop(profile["id"])  # must not raise
