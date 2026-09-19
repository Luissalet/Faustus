"""tests/test_engine_swap.py — src/engine_swap.py: url->engine mapping,
on-demand start (single-flight), autostart off, the idle reaper, and the
`_local_model_slot` wiring in src/llm_core.py.
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

import src.engine_swap as engine_swap
import src.engines as engines
import src.launch_profiles as launch_profiles

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()
    # Engine-swap module state must not leak between tests.
    engine_swap._last_used_mono.clear()
    engine_swap._last_used_wall.clear()
    engine_swap._in_flight.clear()
    engine_swap._first_seen_running.clear()
    engine_swap._starts.clear()
    yield
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()
    engine_swap._last_used_mono.clear()
    engine_swap._last_used_wall.clear()
    engine_swap._in_flight.clear()
    engine_swap._first_seen_running.clear()
    engine_swap._starts.clear()


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


def _make_engine(executable, model_file, port=None):
    return engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=model_file,
        port=port or _free_port(),
    )


def _settings_patch(monkeypatch, **overrides):
    values = {
        "engine_autostart": True,
        "engine_autostart_timeout_s": 5,
        "engine_idle_ttl_minutes": 0,
        "engine_idle_check_s": 30,
    }
    values.update(overrides)

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    from src import settings
    monkeypatch.setattr(settings, "get_setting", fake_get_setting)


# ── url -> engine mapping ───────────────────────────────────────────────────

def test_engine_for_url_matches_localhost_and_127(executable, model_file):
    engine = _make_engine(executable, model_file)
    port = engine["port"]
    found = engine_swap.engine_for_url(f"http://127.0.0.1:{port}/v1/chat/completions")
    assert found is not None and found["id"] == engine["id"]
    found2 = engine_swap.engine_for_url(f"http://localhost:{port}/v1/chat/completions")
    assert found2 is not None and found2["id"] == engine["id"]


def test_engine_for_url_other_port_is_none(executable, model_file):
    engine = _make_engine(executable, model_file)
    other_port = engine["port"] + 1 if engine["port"] < 65000 else engine["port"] - 1
    assert engine_swap.engine_for_url(f"http://127.0.0.1:{other_port}/v1/chat/completions") is None


def test_engine_for_url_non_loopback_is_none(executable, model_file):
    _make_engine(executable, model_file)
    assert engine_swap.engine_for_url("https://api.example.com/v1/chat/completions") is None



def _patch_probe(monkeypatch, fake):
    """Adapt a `probe_llama_cpp`-shaped fake to engine_swap's /health probe."""
    async def probe(engine):
        return "ok" if fake(None).get("healthy") else "down"
    monkeypatch.setattr(engine_swap, "_probe", probe)

# ── ensure_ready: autostart + wait for health ───────────────────────────────

async def test_ensure_ready_starts_stopped_engine_and_waits_for_health(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch)

    started = {"n": 0}

    async def fake_start_engine(engine_id, **kwargs):
        started["n"] += 1
        return {"started": True}

    monkeypatch.setattr(engines, "start_engine", fake_start_engine)

    probe_calls = {"n": 0}

    def fake_probe(root, force=True):
        probe_calls["n"] += 1
        return {"healthy": probe_calls["n"] >= 2}

    _patch_probe(monkeypatch, fake_probe)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(engine_swap.asyncio, "sleep", fast_sleep)

    url = f"http://127.0.0.1:{engine['port']}/v1/chat/completions"
    result = await engine_swap.ensure_ready(url)
    assert result["action"] == "started"
    assert result["engine_id"] == engine["id"]
    assert started["n"] == 1


async def test_ensure_ready_single_flight_two_concurrent_callers(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch)

    started = {"n": 0}
    state = {"start_called": False, "post_start_probes": 0}

    async def fake_start_engine(engine_id, **kwargs):
        started["n"] += 1
        state["start_called"] = True
        await asyncio.sleep(0.05)
        return {"started": True}

    monkeypatch.setattr(engines, "start_engine", fake_start_engine)

    def fake_probe(root, force=True):
        # False for both callers' pre-start check; only turns healthy after
        # the single-flight start has actually run, and after a couple of
        # polls — deterministic regardless of how the two coroutines
        # interleave before either creates the shared start task.
        if not state["start_called"]:
            return {"healthy": False}
        state["post_start_probes"] += 1
        return {"healthy": state["post_start_probes"] >= 2}

    _patch_probe(monkeypatch, fake_probe)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(engine_swap.asyncio, "sleep", fast_sleep)

    url = f"http://127.0.0.1:{engine['port']}/v1/chat/completions"
    results = await asyncio.gather(engine_swap.ensure_ready(url), engine_swap.ensure_ready(url))
    assert started["n"] == 1
    assert all(r["action"] == "started" for r in results)


async def test_ensure_ready_autostart_off_does_not_start(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch, engine_autostart=False)

    started = {"n": 0}

    async def fake_start_engine(engine_id, **kwargs):
        started["n"] += 1
        return {"started": True}

    monkeypatch.setattr(engines, "start_engine", fake_start_engine)

    _patch_probe(monkeypatch, lambda root, force=True: {"healthy": False})

    url = f"http://127.0.0.1:{engine['port']}/v1/chat/completions"
    result = await engine_swap.ensure_ready(url)
    assert result["action"] == "none"
    assert started["n"] == 0


async def test_ensure_ready_non_engine_url_is_noop(monkeypatch):
    result = await engine_swap.ensure_ready("https://api.example.com/v1/chat/completions")
    assert result == {"action": "none"}


# ── idle reaper ──────────────────────────────────────────────────────────────

async def test_reaper_stops_engine_idle_beyond_ttl(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch, engine_idle_ttl_minutes=1)

    _patch_probe(monkeypatch, lambda root, force=True: {"healthy": True})

    stopped = {"n": 0}

    async def fake_stop_engine(engine_id):
        stopped["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(engines, "stop_engine", fake_stop_engine)

    # Idle far beyond the 1-minute TTL.
    engine_swap._last_used_mono[engine["id"]] = engine_swap.time.monotonic() - 120

    await engine_swap._reap_once()
    assert stopped["n"] == 1


async def test_reaper_does_not_stop_engine_with_in_flight_requests(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch, engine_idle_ttl_minutes=1)

    _patch_probe(monkeypatch, lambda root, force=True: {"healthy": True})

    stopped = {"n": 0}

    async def fake_stop_engine(engine_id):
        stopped["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(engines, "stop_engine", fake_stop_engine)

    engine_swap._last_used_mono[engine["id"]] = engine_swap.time.monotonic() - 120
    engine_swap._in_flight[engine["id"]] = 1

    await engine_swap._reap_once()
    assert stopped["n"] == 0


async def test_reaper_ttl_zero_never_stops(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch, engine_idle_ttl_minutes=0)

    _patch_probe(monkeypatch, lambda root, force=True: {"healthy": True})

    stopped = {"n": 0}

    async def fake_stop_engine(engine_id):
        stopped["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(engines, "stop_engine", fake_stop_engine)

    engine_swap._last_used_mono[engine["id"]] = engine_swap.time.monotonic() - 10_000

    await engine_swap._reap_once()
    assert stopped["n"] == 0


# ── _local_model_slot wiring (src/llm_core.py) ──────────────────────────────

async def test_local_model_slot_calls_ensure_ready_for_local_url(monkeypatch, executable, model_file):
    import src.llm_core as llm_core

    engine = _make_engine(executable, model_file)
    url = f"http://127.0.0.1:{engine['port']}/v1/chat/completions"

    calls = []

    async def fake_ensure_ready(target_url, **kwargs):
        calls.append(target_url)
        return {"action": "none"}

    monkeypatch.setattr(engine_swap, "ensure_ready", fake_ensure_ready)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)

    async with llm_core._local_model_slot(url, "some-model", workload="foreground"):
        pass

    assert calls == [url]


async def test_local_model_slot_survives_ensure_ready_exception(monkeypatch, executable, model_file):
    import src.llm_core as llm_core

    engine = _make_engine(executable, model_file)
    url = f"http://127.0.0.1:{engine['port']}/v1/chat/completions"

    async def broken_ensure_ready(target_url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_swap, "ensure_ready", broken_ensure_ready)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)

    entered = False
    async with llm_core._local_model_slot(url, "some-model", workload="foreground"):
        entered = True
    assert entered is True


async def test_ensure_ready_waits_for_a_loading_engine_without_starting(monkeypatch, executable, model_file):
    engine = _make_engine(executable, model_file)
    _settings_patch(monkeypatch)
    started = {"n": 0}

    async def fake_start_engine(engine_id, **kwargs):
        started["n"] += 1
        return {"started": True}

    monkeypatch.setattr(engines, "start_engine", fake_start_engine)
    states = iter(["loading", "loading", "ok"])

    async def probe(_engine):
        return next(states, "ok")

    monkeypatch.setattr(engine_swap, "_probe", probe)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(engine_swap.asyncio, "sleep", fast_sleep)
    result = await engine_swap.ensure_ready(f"http://localhost:{engine['port']}/v1")
    assert result["action"] == "waited"
    assert started["n"] == 0
