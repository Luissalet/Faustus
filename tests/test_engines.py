"""src/engines.py — the local inference engine (llama.cpp's `llama-server`)
managed from the UI, built on `src.launch_profiles`.

Real spawns are never made (COMUN rule 8): `launch_profiles.spawn_detached`
is monkeypatched exactly the way `tests/test_launch_profiles.py` does it.
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

import src.engines as engines
import src.launch_profiles as launch_profiles
import src.process_center as process_center
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
    path = tmp_path / "llama-server"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def model_file(tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"x" * 4096)
    return str(path)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mock_spawn(monkeypatch, *, pid=None, spawned_at=None):
    calls = []
    pid = pid if pid is not None else os.getpid()

    def fake_spawn(argv, *, cwd, env, log_path, owner=""):
        calls.append({"argv": argv, "cwd": cwd, "env": env, "log_path": log_path, "owner": owner})
        return process_launch.LaunchResult(pid=pid, log_path=log_path, spawned_at=spawned_at)

    monkeypatch.setattr(launch_profiles, "spawn_detached", fake_spawn)
    return calls


# ── config CRUD ──────────────────────────────────────────────────────────────

def test_create_engine_round_trips_structured_fields(executable, model_file):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="Local 27B", executable=executable, model_path=model_file,
        ctx_size=8192, port=port, extra_args=["--flash-attn"],
    )
    assert engine["model_path"] == model_file
    assert engine["ctx_size"] == 8192
    assert engine["port"] == port
    assert engine["extra_args"] == ["--flash-attn"]
    assert engine["kind"] == "process"

    fetched = engines.get_engine(engine["id"])
    assert fetched["port"] == port
    assert any(e["id"] == engine["id"] for e in engines.list_engines())


def test_create_engine_rejects_relative_model_path(executable):
    with pytest.raises(engines.EngineValidationError):
        engines.create_engine(
            owner="tester", name="X", executable=executable, model_path="model.gguf",
            port=_free_port(),
        )


def test_create_engine_rejects_non_llama_server_executable(tmp_path, model_file):
    other = tmp_path / "python3"
    other.write_text("x")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(engines.EngineValidationError):
        engines.create_engine(
            owner="tester", name="X", executable=str(other), model_path=model_file,
            port=_free_port(),
        )


def test_update_engine_rebuilds_argv(executable, model_file, tmp_path):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=port,
    )
    new_model = tmp_path / "other.gguf"
    new_model.write_bytes(b"y" * 10)
    updated = engines.update_engine(engine["id"], model_path=str(new_model), ctx_size=16384)
    assert updated["model_path"] == str(new_model)
    assert updated["ctx_size"] == 16384
    assert "-m" in updated["argv"] and str(new_model) in updated["argv"]


def test_delete_engine_removes_it(executable, model_file):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=_free_port(),
    )
    assert engines.delete_engine(engine["id"]) is True
    assert engines.get_engine(engine["id"]) is None
    assert engines.delete_engine(engine["id"]) is False


def test_non_engine_launch_profile_is_invisible_to_engines(tmp_path):
    other_exe = tmp_path / "node"
    other_exe.write_text("x")
    other_exe.chmod(other_exe.stat().st_mode | stat.S_IEXEC)
    profile = launch_profiles.create_profile(
        owner="tester", name="Not an engine", kind="process", executable=str(other_exe),
        argv=[], cwd=str(tmp_path),
    )
    assert engines.get_engine(profile["id"]) is None
    assert profile["id"] not in [e["id"] for e in engines.list_engines()]


# ── start: port-busy refusal ─────────────────────────────────────────────────

async def test_start_refused_when_port_already_listening(monkeypatch, executable, model_file):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=port,
    )
    calls = _mock_spawn(monkeypatch)
    monkeypatch.setattr(
        process_center, "pid_listening_on",
        lambda p, ports_by_pid=None: {"pid": 4242, "created_at": 1.0, "cmdline": "something-else"},
    )
    result = await engines.start_engine(engine["id"])
    assert result["started"] is False
    assert "already in use" in result["error"]
    assert result["held_by"]["pid"] == 4242
    assert calls == []  # never spawned


async def test_start_refused_when_vram_insufficient(monkeypatch, executable, model_file):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=port,
    )
    calls = _mock_spawn(monkeypatch)
    monkeypatch.setattr(process_center, "pid_listening_on", lambda p, ports_by_pid=None: None)

    from src import gpu_shared_memory
    monkeypatch.setattr(gpu_shared_memory, "vram_snapshot",
                         lambda: {"supported": True, "free": 0, "total": 100, "used": 100})
    result = await engines.start_engine(engine["id"])
    assert result["started"] is False
    assert "VRAM" in result["error"]
    assert calls == []


async def test_start_succeeds_when_port_free_and_no_gpu_reading(monkeypatch, executable, model_file):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=port,
    )
    calls = _mock_spawn(monkeypatch, pid=os.getpid(), spawned_at=None)
    monkeypatch.setattr(process_center, "pid_listening_on", lambda p, ports_by_pid=None: None)

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)
    result = await engines.start_engine(engine["id"])
    assert result["started"] is True
    assert len(calls) == 1
    assert calls[0]["argv"][:3] == [executable, "-m", model_file]


# ── stop: kills the tree via process_center ──────────────────────────────────

async def test_stop_engine_delegates_to_process_center_stop_port(monkeypatch, executable, model_file):
    port = _free_port()
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=port,
    )
    seen = {}

    def fake_stop_port(p, allow_protected=False):
        seen["port"] = p
        return {"ok": True, "code": "", "reason": "", "signalled": [123, 124], "refused": []}

    monkeypatch.setattr(process_center, "stop_port", fake_stop_port)
    result = await engines.stop_engine(engine["id"])
    assert seen["port"] == port
    assert result["stopped"] is True
    assert result["signalled"] == [123, 124]


async def test_stop_engine_without_port_is_refused(executable, model_file):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=_free_port(),
    )
    launch_profiles.update_profile(engine["id"], argv=[], readiness={})
    result = await engines.stop_engine(engine["id"])
    assert result["ok"] is False
    assert result["code"] == "no_port"


# ── status: from the health probe, never "the process exists" ───────────────

async def test_status_reports_stopped_when_health_probe_fails(monkeypatch, executable, model_file):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=_free_port(),
    )
    from src import runner_providers
    monkeypatch.setattr(runner_providers, "probe_llama_cpp",
                         lambda root, force=False, timeout=1.5: {
                             "available": False, "healthy": False, "model": "", "context_length": 0,
                             "footprint_bytes": None, "footprint_measured": False, "generating": False,
                         })
    status = await engines.status_engine(engine["id"])
    assert status["state"] == "stopped"


async def test_status_reports_running_only_when_probe_is_healthy(monkeypatch, executable, model_file):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=_free_port(),
    )
    from src import runner_providers
    monkeypatch.setattr(runner_providers, "probe_llama_cpp",
                         lambda root, force=False, timeout=1.5: {
                             "available": True, "healthy": True, "model": "local-27b", "context_length": 8192,
                             "footprint_bytes": 4096, "footprint_measured": True, "generating": False,
                         })
    status = await engines.status_engine(engine["id"])
    assert status["state"] == "running"
    assert status["model"] == "local-27b"
    assert status["context_length"] == 8192


async def test_status_reports_unhealthy_when_process_exists_but_probe_unhealthy(monkeypatch, executable, model_file):
    """A pid on the port proves nothing by itself — only the health probe
    decides 'running'."""
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file, port=_free_port(),
    )
    from src import runner_providers
    monkeypatch.setattr(runner_providers, "probe_llama_cpp",
                         lambda root, force=False, timeout=1.5: {
                             "available": True, "healthy": False, "model": "", "context_length": 0,
                             "footprint_bytes": None, "footprint_measured": False, "generating": False,
                         })
    status = await engines.status_engine(engine["id"])
    assert status["state"] == "unhealthy"
