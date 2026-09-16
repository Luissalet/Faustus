"""Pin Ollama keep_alive for the length of a local agent run."""

from __future__ import annotations

import pytest

from src import run_model_pin as pin


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    pin.reset_for_tests()
    monkeypatch.setattr(pin, "restore_keep_alive", lambda *a, **k: True)
    yield
    pin.reset_for_tests()


def test_pin_supplies_keep_alive_for_that_run():
    pin.pin_for_run("r1", "http://127.0.0.1:11434", "qwen3.8:27b")
    assert pin.keep_alive_override("r1") == "2h"
    assert pin.keep_alive_override("other") is None
    rec = pin.unpin_for_run("r1")
    assert rec["model"] == "qwen3.8:27b"
    assert pin.keep_alive_override("r1") is None


def test_keep_alive_override_uses_active_context():
    pin.pin_for_run("r2", "http://127.0.0.1:11434/v1", "m")
    assert pin.keep_alive_override() == "2h"
    pin.unpin_for_run("r2")
    assert pin.keep_alive_override() is None


def test_unpin_restores_saved_keep_alive(monkeypatch):
    calls = []

    def fake_restore(endpoint, model, keep_alive):
        calls.append((endpoint, model, keep_alive))
        return True

    monkeypatch.setattr(pin, "restore_keep_alive", fake_restore)
    monkeypatch.setattr(pin, "_saved_keep_alive", lambda endpoint, model: "5m")
    pin.pin_for_run("r3", "http://127.0.0.1:11434/v1", "qwen")
    pin.unpin_for_run("r3")
    assert calls == [("http://127.0.0.1:11434/v1", "qwen", "5m")]


def test_with_model_defaults_pin_beats_saved_keep_alive(monkeypatch):
    from src import llm_core

    monkeypatch.setattr(llm_core, "_model_load_defaults", lambda url, model: {"keep_alive": "5m", "num_ctx": 8192})
    pin.pin_for_run("r4", "http://127.0.0.1:11434", "m")
    merged = llm_core._with_model_defaults("http://127.0.0.1:11434/v1", "m", None)
    assert merged["keep_alive"] == "2h"
    assert merged["num_ctx"] == 8192
    caller = llm_core._with_model_defaults("http://127.0.0.1:11434/v1", "m", {"keep_alive": "30m"})
    assert caller["keep_alive"] == "30m"
    pin.unpin_for_run("r4")
