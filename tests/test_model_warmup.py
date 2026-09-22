"""src/model_warmup.py: the default local model is loaded at startup with
keep_alive -1 and re-pinned; remote defaults are left alone."""
import asyncio

import pytest

from src import model_warmup as mw


def test_only_local_ollama_endpoints_are_warmed():
    assert mw._ollama_root("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert mw._ollama_root("http://localhost:11434/api/chat") == "http://localhost:11434"
    assert mw._ollama_root("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080"   # a local Ollama on another port
    assert mw._ollama_root("https://api.openai.com/v1") is None
    assert mw._ollama_root("https://openrouter.ai/api/v1") is None
    assert mw._keep_alive_value("-1") == -1 and mw._keep_alive_value("2h") == "2h" and mw._keep_alive_value(600) == 600


def test_warm_once_posts_a_promptless_generate_with_keep_alive(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(mw, "resolve_default", lambda: {"url": "http://127.0.0.1:11434/v1", "model": "qwen:27b", "root": "http://127.0.0.1:11434"})
    monkeypatch.setattr(mw, "_settings", lambda: {"enabled": True, "keep_alive": "-1", "every_s": 600.0})
    out = asyncio.run(mw.warm_once())
    # The ping carries the resident runner's own load options since
    # 20-09-2026: a bare ping takes Ollama's default num_ctx, and a runner
    # loaded with another window treats that as a reload -- it was evicting
    # the 27B at the end of every run. This used to pin the body exactly and
    # went red the moment those options were added, saying nothing about the
    # two things it exists to check: where the ping goes, and that it carries
    # no prompt and the right keep_alive.
    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://127.0.0.1:11434/api/generate"
    assert body["model"] == "qwen:27b" and body["keep_alive"] == -1
    assert not body.get("prompt")
    assert set(body) <= {"model", "keep_alive", "prompt", "stream", "options"}
    assert out["ok"] is True and out["model"] == "qwen:27b"


def test_warm_once_without_a_local_default_does_nothing(monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    out = asyncio.run(mw.warm_once())
    assert out["ok"] is None and "no local default" in out["detail"]


def test_settings_defaults_exist():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["warm_default_model"] is True
    assert DEFAULT_SETTINGS["warm_default_model_keep_alive"] == "-1"
    # X-B: the residency keeper checks every 20s by default now; the old
    # 120s value must still be a valid, accepted setting (not validated here,
    # just not hardcoded as the only legal value).
    assert DEFAULT_SETTINGS["warm_default_model_every_s"] == 20


# ── residency keeper (check_once) ───────────────────────────────────────────

_TARGET = {"url": "http://127.0.0.1:11434/v1", "model": "qwen3.8:27b", "root": "http://127.0.0.1:11434"}


def _reset_state():
    mw._keeper.update({"last_check": None, "resident": None, "expires_at": "", "reloads": 0,
                       "repins": 0, "yielding_to": None, "waiting_for_room": False,
                       "backend": None, "resident_since": None})
    mw._yield_logged = False
    mw._fit_wait_logged = False


@pytest.fixture(autouse=True)
def _reset_keeper():
    _reset_state()
    yield
    _reset_state()


def _fake_ps(models):
    async def _ps(root):
        return {"models": models}
    return _ps


def test_check_once_reloads_when_not_resident(monkeypatch):
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([]))  # gone from /api/ps
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": True})

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == [True]
    assert out["resident"] is True and out["reloads"] == 1


def test_check_once_waits_when_another_load_is_in_flight(monkeypatch):
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: True)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([]))

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == []  # did not contend for the VRAM another load needs
    assert out["resident"] is False and out["reloads"] == 0


def test_check_once_repins_when_keep_alive_shrank(monkeypatch):
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    # Resident, but expires_at is a few minutes out — another client's
    # default keep_alive (commonly 5m) shortened our -1.
    monkeypatch.setattr(mw, "_api_ps", _fake_ps(
        [{"name": "qwen3.8:27b", "expires_at": "2026-09-18T00:05:00Z"}]))

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == [True]
    assert out["resident"] is True and out["repins"] == 1


def test_check_once_does_nothing_when_resident_forever(monkeypatch):
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps(
        [{"name": "qwen3.8:27b", "expires_at": "0001-01-01T00:00:00Z"}]))

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == []
    assert out["resident"] is True and out["reloads"] == 0 and out["repins"] == 0


def test_is_forever_matches_studio_heuristic():
    assert mw._is_forever("0001-01-01T00:00:00Z") is True
    assert mw._is_forever("9999-12-31T23:59:59Z") is True
    assert mw._is_forever("2026-09-18T00:05:00Z") is False
    assert mw._is_forever("") is False


def test_check_once_pins_the_default_model(monkeypatch):
    pinned = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: pinned.append((root, model)))
    monkeypatch.setattr(mw, "_api_ps", _fake_ps(
        [{"name": "qwen3.8:27b", "expires_at": "0001-01-01T00:00:00Z"}]))
    asyncio.run(mw.check_once())
    assert pinned == [(_TARGET["root"], _TARGET["model"])]


# ── X-D: the default yields to an explicitly-picked model, on its own ──────

def test_embedding_name_heuristic():
    assert mw._looks_like_embedding_model("nomic-embed-text") is True
    assert mw._looks_like_embedding_model("mxbai-embed-large:latest") is True
    assert mw._looks_like_embedding_model("bge-m3") is True
    assert mw._looks_like_embedding_model("qwen3.8:27b") is False


def test_check_once_waits_while_another_active_model_is_resident(monkeypatch):
    """The owner explicitly picked another model (X-D, 08-09-2026 regression):
    it is resident, was used seconds ago, and is neither the default nor an
    embedding model — the keeper must not reload the default over it."""
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps(
        [{"name": "qwen3-coder:30b-q8_0"}]))  # default itself gone
    monkeypatch.setattr("src.vram_admission.last_active_seconds",
                        lambda root, model: 5.0 if model == "qwen3-coder:30b-q8_0" else None)

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == []
    assert out["resident"] is False
    assert out["reloads"] == 0
    assert out["yielding_to"] == "qwen3-coder:30b-q8_0"


def test_check_once_ignores_an_active_embedding_model(monkeypatch):
    """An embedding model resident and busy is not "the owner picked another
    chat model" — the default reloads normally."""
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([{"name": "nomic-embed-text"}]))
    monkeypatch.setattr("src.vram_admission.last_active_seconds", lambda root, model: 1.0)
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": True})

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == [True]
    assert out["reloads"] == 1
    assert out["yielding_to"] is None


def test_check_once_reloads_after_the_other_model_goes_idle(monkeypatch):
    """Past `warm_default_model_yield_minutes`, the other model no longer
    counts as active — the default comes back."""
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_settings", lambda: {"enabled": True, "keep_alive": "-1",
                                                   "every_s": 20.0, "yield_minutes": 10.0})
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([{"name": "qwen3-coder:30b-q8_0"}]))
    # 700s > 600s (10 minutes): no longer "active".
    monkeypatch.setattr("src.vram_admission.last_active_seconds", lambda root, model: 700.0)
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": True})

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == [True]
    assert out["reloads"] == 1
    assert out["yielding_to"] is None


def test_check_once_waits_when_default_would_not_fit(monkeypatch):
    """Even once the other model is gone/idle, never reload into a
    shortfall — wait and retry next cycle instead of spilling to CPU/PCIe."""
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([]))  # nothing resident at all
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": False})

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    out = asyncio.run(mw.check_once())
    assert warmed == []
    assert out["reloads"] == 0
    assert out["waiting_for_room"] is True

    # Retried next cycle: once it fits, it reloads.
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": True})
    out2 = asyncio.run(mw.check_once())
    assert warmed == [True]
    assert out2["reloads"] == 1
    assert out2["waiting_for_room"] is False


def test_is_default_matches_run_model_pin_and_vram_admission(monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    assert mw.is_default("http://127.0.0.1:11434", "qwen3.8:27b") is True
    assert mw.is_default("http://127.0.0.1:11434/v1/chat/completions", "qwen3.8:27b:latest") is True
    assert mw.is_default("http://127.0.0.1:11434", "some-other-model") is False


# ── the residency switch: on/off, live effect, both backends ───────────────

_ENGINE = {"id": "eng-1", "host": "127.0.0.1", "port": 8082, "model_path": "/models/x.gguf", "name": "X"}


def test_set_residency_on_persists_setting_and_loads_now(monkeypatch):
    saved = []
    monkeypatch.setattr("src.settings.update_settings", lambda patch: saved.append(patch))
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([{"name": "qwen3.8:27b", "expires_at": "0001-01-01T00:00:00Z"}]))

    out = asyncio.run(mw.set_residency(True))
    assert saved == [{"warm_default_model": True}]
    assert out["enabled"] is True
    assert out["resident"] is True


def test_set_residency_off_persists_setting_and_releases_now(monkeypatch):
    saved = []
    released = []
    monkeypatch.setattr("src.settings.update_settings", lambda patch: saved.append(patch))
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr("src.vram_admission.unpin_model", lambda root, model: released.append((root, model)))

    class Resp:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            released.append(("post", url, json))
            return Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)

    out = asyncio.run(mw.set_residency(False))
    assert saved == [{"warm_default_model": False}]
    assert out["enabled"] is False
    assert out["resident"] is False
    assert (_TARGET["root"], _TARGET["model"]) in released
    assert ("post", _TARGET["root"] + "/api/generate", {"model": _TARGET["model"], "keep_alive": 0}) in released


def test_residency_status_reports_backend_and_since(monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_settings", lambda: {"enabled": True, "keep_alive": "-1",
                                                   "every_s": 20.0, "yield_minutes": 10.0})
    mw._keeper.update({"resident": True, "resident_since": 123.0, "backend": "ollama"})
    out = mw.residency_status()
    assert out == {
        "enabled": True, "loaded": True, "since": 123.0, "backend": "ollama",
        "backend_label": "Ollama", "model": "qwen3.8:27b", "last_check": None,
    }


# ── llama.cpp residency: check_once() reaches _check_once_llamacpp when the
# default is not an Ollama endpoint but is a managed engine ────────────────

def test_check_once_starts_llamacpp_engine_when_not_healthy(monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    monkeypatch.setattr(mw, "_resolve_default_engine", lambda: dict(_ENGINE))
    monkeypatch.setattr("src.vram_admission.pin_model", lambda root, model: None)

    import src.engine_swap as engine_swap
    started = []

    async def fake_probe_healthy(engine):
        return False
    monkeypatch.setattr(engine_swap, "probe_healthy", fake_probe_healthy)
    touched = []
    monkeypatch.setattr(engine_swap, "touch", lambda engine_id: touched.append(engine_id))

    import src.engines as engines

    async def fake_start_engine(engine_id):
        started.append(engine_id)
        return {"started": True}
    monkeypatch.setattr(engines, "start_engine", fake_start_engine)

    out = asyncio.run(mw.check_once())
    assert started == ["eng-1"]
    assert touched == ["eng-1"]
    assert out["resident"] is True
    assert out["backend"] == "llamacpp"
    assert out["reloads"] == 1


def test_check_once_exempts_healthy_llamacpp_engine_from_idle_reaper(monkeypatch):
    """While residency is on and the engine is already healthy, each cycle
    touches it — resetting engine_swap's own idle clock — instead of
    starting a second copy of it."""
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    monkeypatch.setattr(mw, "_resolve_default_engine", lambda: dict(_ENGINE))
    monkeypatch.setattr("src.vram_admission.pin_model", lambda root, model: None)

    import src.engine_swap as engine_swap

    async def fake_probe_healthy(engine):
        return True
    monkeypatch.setattr(engine_swap, "probe_healthy", fake_probe_healthy)
    touched = []
    monkeypatch.setattr(engine_swap, "touch", lambda engine_id: touched.append(engine_id))

    import src.engines as engines
    started = []

    async def fake_start_engine(engine_id):
        started.append(engine_id)
        return {"started": True}
    monkeypatch.setattr(engines, "start_engine", fake_start_engine)

    out = asyncio.run(mw.check_once())
    assert started == []  # already healthy: never re-started
    assert touched == ["eng-1"]
    assert out["resident"] is True
    assert out["backend"] == "llamacpp"


def test_set_residency_off_stops_idle_llamacpp_engine(monkeypatch):
    saved = []
    monkeypatch.setattr("src.settings.update_settings", lambda patch: saved.append(patch))
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    monkeypatch.setattr(mw, "_resolve_default_engine", lambda: dict(_ENGINE))
    monkeypatch.setattr("src.vram_admission.unpin_model", lambda root, model: None)

    import src.engine_swap as engine_swap
    monkeypatch.setattr(engine_swap, "status", lambda: {"engines": {"eng-1": {"in_flight": 0}}})

    import src.engines as engines
    stopped = []

    async def fake_stop_engine(engine_id):
        stopped.append(engine_id)
        return {"ok": True}
    monkeypatch.setattr(engines, "stop_engine", fake_stop_engine)

    out = asyncio.run(mw.set_residency(False))
    assert saved == [{"warm_default_model": False}]
    assert stopped == ["eng-1"]
    assert out["resident"] is False


def test_set_residency_off_never_stops_an_in_flight_llamacpp_engine(monkeypatch):
    """Turning the switch off must not yank the engine from under an
    in-flight chat turn happening to use the default model right now."""
    monkeypatch.setattr("src.settings.update_settings", lambda patch: None)
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    monkeypatch.setattr(mw, "_resolve_default_engine", lambda: dict(_ENGINE))
    monkeypatch.setattr("src.vram_admission.unpin_model", lambda root, model: None)

    import src.engine_swap as engine_swap
    monkeypatch.setattr(engine_swap, "status", lambda: {"engines": {"eng-1": {"in_flight": 1}}})

    import src.engines as engines
    stopped = []

    async def fake_stop_engine(engine_id):
        stopped.append(engine_id)
        return {"ok": True}
    monkeypatch.setattr(engines, "stop_engine", fake_stop_engine)

    asyncio.run(mw.set_residency(False))
    assert stopped == []
    assert mw.is_default("http://10.0.0.5:11434", "qwen3.8:27b") is False
