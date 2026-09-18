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
    assert calls == [("http://127.0.0.1:11434/api/generate", {"model": "qwen:27b", "keep_alive": -1})]
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


@pytest.fixture(autouse=True)
def _reset_keeper():
    mw._keeper.update({"last_check": None, "resident": None, "expires_at": "", "reloads": 0, "repins": 0})
    yield
    mw._keeper.update({"last_check": None, "resident": None, "expires_at": "", "reloads": 0, "repins": 0})


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
