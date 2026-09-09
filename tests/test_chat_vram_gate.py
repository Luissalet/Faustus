"""OBJ-1, the chat side: a turn on a local model passes the VRAM gate first.

Ollama never says no: a model that does not fit next to what is resident is
loaded anyway, spilling to CPU/PCIe, and the only symptom is a turn ten
times slower — which is how two 27Bs ended up stacked on 08-09-2026. The
chat now runs src.vram_admission.admit before its first call and streams
what the gate says (`vram_admission` events), so the screen shows the same
dialog research and the Load button show; a cancelled load ends the turn
with a reason instead of loading regardless.
"""
import asyncio
import json

import pytest

from routes import chat_routes
from src import agent_runs
from src.vram_admission import AdmissionCancelled

LOCAL = "http://127.0.0.1:11434/v1"
REMOTE = "https://api.openai.com/v1"


def _events(gen):
    out = []

    async def _run():
        async for ev in gen:
            out.append(json.loads(ev[6:]))
    asyncio.run(_run())
    return out


def test_a_remote_endpoint_is_not_gated(monkeypatch):
    called = []

    async def _admit(*a, **k):
        called.append(1)
        return "proceed"
    monkeypatch.setattr("src.vram_admission.admit", _admit)
    outcome = {"ok": True, "error": ""}
    assert _events(chat_routes._vram_admission_events(REMOTE, "gpt-4o", "luis", outcome)) == []
    assert outcome["ok"] and not called


def test_the_gate_s_progress_is_streamed_and_the_turn_goes_on(monkeypatch):
    async def _admit(url, model, *, owner="", on_progress=None, **k):
        on_progress({"phase": "vram_blocked", "ticket": "t1", "message": f"{model} does not fit in VRAM",
                     "residents": [{"name": "qwen3.8:27b-q8_0"}], "short_by_bytes": 12 * 2**30})
        await asyncio.sleep(0)
        on_progress({"phase": "unloading_model", "message": "Unloading qwen3.8:27b-q8_0…", "names": ["qwen3.8:27b-q8_0"]})
        return "proceed"
    monkeypatch.setattr("src.vram_admission.admit", _admit)
    outcome = {"ok": True, "error": ""}
    events = _events(chat_routes._vram_admission_events(LOCAL, "qwen3.8:27b-q4_K_M", "luis", outcome))
    assert [e["type"] for e in events] == ["vram_admission", "vram_admission"]
    assert events[0]["data"]["phase"] == "vram_blocked" and events[0]["data"]["ticket"] == "t1"
    assert events[1]["data"]["phase"] == "unloading_model"
    assert outcome["ok"]


def test_a_cancelled_load_ends_the_turn_with_the_reason(monkeypatch):
    async def _admit(url, model, *, owner="", on_progress=None, **k):
        on_progress({"phase": "error", "message": f"Load of {model} cancelled: it does not fit in VRAM."})
        raise AdmissionCancelled(f"Load of {model} cancelled by luis: it does not fit in VRAM next to what is loaded.")
    monkeypatch.setattr("src.vram_admission.admit", _admit)
    outcome = {"ok": True, "error": ""}
    events = _events(chat_routes._vram_admission_events(LOCAL, "qwen3.8:27b-q4_K_M", "luis", outcome))
    assert events[-1]["data"]["phase"] == "error"
    assert outcome["ok"] is False
    assert "cancelled by luis" in outcome["error"]


def test_a_broken_gate_never_costs_the_turn(monkeypatch):
    async def _admit(*a, **k):
        raise RuntimeError("ollama is on fire")
    monkeypatch.setattr("src.vram_admission.admit", _admit)
    outcome = {"ok": True, "error": ""}
    _events(chat_routes._vram_admission_events(LOCAL, "m", "luis", outcome))
    assert outcome["ok"]


# ── the heartbeat says what the model is doing ──────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _Client:
    payload = {"models": []}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        assert url.endswith("/api/ps")
        return _Resp(_Client.payload)


@pytest.fixture
def ps(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    agent_runs._MODEL_STATE_CACHE.clear()
    return _Client


def _run(model, url):
    run = agent_runs._Run()
    run.model, run.endpoint_url = model, url
    return run


def test_a_model_not_resident_yet_is_loading(ps):
    ps.payload = {"models": []}
    assert asyncio.run(agent_runs.model_state(_run("qwen3.8:27b-q4_K_M", LOCAL))) == {"resident": False}


def test_a_resident_model_on_the_gpu_is_reading_the_context(ps):
    ps.payload = {"models": [{"name": "qwen3.8:27b-q4_K_M", "size": 26_000_000_000, "size_vram": 26_000_000_000, "context_length": 65536}]}
    state = asyncio.run(agent_runs.model_state(_run("qwen3.8:27b-q4_K_M", LOCAL)))
    assert state["resident"] is True and state["spill"] is False and state["context"] == 65536


def test_a_model_spilling_to_ram_says_so(ps):
    ps.payload = {"models": [{"name": "qwen3.8:27b-q8_0", "size": 33_000_000_000, "size_vram": 29_000_000_000}]}
    state = asyncio.run(agent_runs.model_state(_run("qwen3.8:27b-q8_0", LOCAL)))
    assert state["spill"] is True and state["vram_bytes"] == 29_000_000_000


def test_remote_endpoints_and_unknown_runs_say_nothing(ps):
    assert asyncio.run(agent_runs.model_state(_run("gpt-4o", REMOTE))) is None
    assert asyncio.run(agent_runs.model_state(_run("", LOCAL))) is None
