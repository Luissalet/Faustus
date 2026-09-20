"""Pin Ollama keep_alive for the length of a local agent run."""

from __future__ import annotations

import asyncio

import pytest

from src import run_model_pin as pin


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    pin.reset_for_tests()
    monkeypatch.setattr(pin, "restore_keep_alive", lambda *a, **k: True)
    yield
    pin.reset_for_tests()


def test_pin_supplies_keep_alive_for_the_pinned_model_only():
    tok = pin.pin_for_run("r1", "http://127.0.0.1:11434", "qwen3.8:27b")
    assert pin.keep_alive_override("qwen3.8:27b") == "2h"
    assert pin.keep_alive_override("QWEN3.8:27b:latest") == "2h"
    # A judge / embedding model called inside the run keeps its own keep_alive.
    assert pin.keep_alive_override("nomic-embed-text") is None
    rec = pin.unpin_for_run("r1", tok)
    assert rec["model"] == "qwen3.8:27b"
    assert pin.keep_alive_override("qwen3.8:27b") is None


def test_keep_alive_override_uses_active_context():
    tok = pin.pin_for_run("r2", "http://127.0.0.1:11434/v1", "m")
    assert pin.keep_alive_override() == "2h"
    pin.unpin_for_run("r2", tok)
    assert pin.keep_alive_override() is None


def test_nested_run_under_same_session_does_not_drop_parent_pin():
    """A worker spawned by the run shares the session id: its unpin must
    restore the parent's pin, not clear it."""
    parent = pin.pin_for_run("sess", "http://127.0.0.1:11434", "big")
    child = pin.pin_for_run("sess", "http://127.0.0.1:11434", "coder")
    assert pin.keep_alive_override("coder") == "2h"
    assert pin.keep_alive_override("big") is None  # the child context pins its own model
    pin.unpin_for_run("sess", child)
    assert pin.keep_alive_override("big") == "2h"
    pin.unpin_for_run("sess", parent)
    assert pin.keep_alive_override("big") is None


def test_unpin_restores_saved_keep_alive(monkeypatch):
    calls = []

    def fake_restore(endpoint, model, keep_alive):
        calls.append((endpoint, model, keep_alive))
        return True

    monkeypatch.setattr(pin, "restore_keep_alive", fake_restore)
    monkeypatch.setattr(pin, "_saved_keep_alive", lambda endpoint, model: "5m")
    tok = pin.pin_for_run("r3", "http://127.0.0.1:11434/v1", "qwen")
    pin.unpin_for_run("r3", tok)
    assert calls == [("http://127.0.0.1:11434/v1", "qwen", "5m")]


def test_async_unpin_runs_restore_off_the_loop(monkeypatch):
    import threading

    seen = {}

    def fake_restore(endpoint, model, keep_alive):
        seen["thread"] = threading.current_thread().name
        return True

    monkeypatch.setattr(pin, "restore_keep_alive", fake_restore)
    monkeypatch.setattr(pin, "_saved_keep_alive", lambda endpoint, model: "5m")

    async def go():
        tok = pin.pin_for_run("r5", "http://127.0.0.1:11434", "m")
        assert pin.keep_alive_override("m") == "2h"
        rec = await pin.unpin_for_run_async("r5", tok)
        assert rec["model"] == "m"
        assert pin.keep_alive_override("m") is None

    asyncio.run(go())
    assert seen["thread"] != threading.main_thread().name


def test_restore_never_talks_to_a_remote_provider(monkeypatch):
    pin.reset_for_tests()
    monkeypatch.undo()  # real restore_keep_alive
    posted = []

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    class _Http:
        @staticmethod
        def post(url, **kw):
            posted.append(url)

        @staticmethod
        def get(url, **kw):
            return _Resp({"models": [{"name": "qwen:latest"}]})

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Http)
    assert pin.restore_keep_alive("https://openrouter.ai/api/v1", "gpt", "5m") is False
    assert posted == []
    assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "qwen", "5m") is True
    assert posted == ["http://127.0.0.1:11434/api/generate"]


def test_with_model_defaults_pin_beats_saved_keep_alive(monkeypatch):
    from src import llm_core

    monkeypatch.setattr(llm_core, "_model_load_defaults", lambda url, model: {"keep_alive": "5m", "num_ctx": 8192})
    tok = pin.pin_for_run("r4", "http://127.0.0.1:11434", "m")
    merged = llm_core._with_model_defaults("http://127.0.0.1:11434/v1", "m", None)
    assert merged["keep_alive"] == "2h"
    assert merged["num_ctx"] == 8192
    caller = llm_core._with_model_defaults("http://127.0.0.1:11434/v1", "m", {"keep_alive": "30m"})
    assert caller["keep_alive"] == "30m"
    other = llm_core._with_model_defaults("http://127.0.0.1:11434/v1", "judge", None)
    assert other["keep_alive"] == "5m"
    pin.unpin_for_run("r4", tok)


def test_restore_never_shortens_the_default_model_below_forever(monkeypatch):
    """Owner's rule: whatever an agent run pinned, restoring afterwards must
    never leave the DEFAULT model with a keep_alive shorter than -1 — the
    residency keeper re-pins it too, but this path must not even try."""
    pin.reset_for_tests()
    monkeypatch.undo()
    posted = []

    class _Resp:
        def json(self):
            return {"models": [{"name": "qwen3.8:27b"}]}

    class _Http:
        @staticmethod
        def post(url, **kw):
            posted.append((url, kw.get("json")))

        @staticmethod
        def get(url, **kw):
            return _Resp()

    import sys
    from src import model_warmup

    monkeypatch.setitem(sys.modules, "httpx", _Http)
    monkeypatch.setattr(model_warmup, "resolve_default", lambda: {
        "url": "http://127.0.0.1:11434/v1", "model": "qwen3.8:27b", "root": "http://127.0.0.1:11434",
    })
    # Something (a saved per-model keep_alive, another app's default) tries
    # to restore the default model to a short-lived "5m" — it must land as -1.
    assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "qwen3.8:27b", "5m") is True
    assert len(posted) == 1
    url, body = posted[0]
    assert url == "http://127.0.0.1:11434/api/generate"
    assert body["keep_alive"] == -1

    # A different (non-default) model is restored to whatever was asked.
    posted.clear()
    monkeypatch.setattr(_Resp, "json", lambda self: {"models": [{"name": "coder:7b"}]})
    assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "coder:7b", "5m") is True
    assert posted[0][1]["keep_alive"] == "5m"


def test_restore_skips_a_model_that_is_no_longer_resident(monkeypatch):
    """An empty-prompt generate LOADS a model: never restore keep_alive on
    one Ollama already unloaded (that would pull 18 GB back for nothing)."""
    pin.reset_for_tests()
    monkeypatch.undo()
    posted = []

    class _Resp:
        def json(self):
            return {"models": [{"name": "other:7b"}]}

    class _Http:
        @staticmethod
        def post(url, **kw):
            posted.append(url)

        @staticmethod
        def get(url, **kw):
            return _Resp()

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Http)
    assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "qwen", "5m") is False
    assert posted == []


def test_restore_ping_echoes_the_resident_context_so_ollama_does_not_reload(monkeypatch):
    """A keep_alive ping without the runner's num_ctx makes Ollama reload the
    model with its default window; the 3 s timeout then aborts that load and
    the model ends up evicted after every run (seen live on 20-09-2026 with a
    27B loaded at 200k). The ping must carry the resident context length."""
    pin.reset_for_tests()
    monkeypatch.undo()
    posted = []

    class _Resp:
        def json(self):
            return {"models": [{"name": "qwen3.8:27b-q4_K_M", "context_length": 200192}]}

    class _Http:
        @staticmethod
        def post(url, **kw):
            posted.append(kw.get("json"))

        @staticmethod
        def get(url, **kw):
            return _Resp()

    import sys
    from src import model_load_options

    monkeypatch.setitem(sys.modules, "httpx", _Http)
    monkeypatch.setattr(model_load_options, "resolve_for_request",
                        lambda url, model, **kw: {"num_ctx": 8192, "num_gpu": 99})
    assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M", "5m") is True
    assert posted[0]["options"]["num_ctx"] == 200192  # the runner's real window wins
    assert posted[0]["options"]["num_gpu"] == 99
