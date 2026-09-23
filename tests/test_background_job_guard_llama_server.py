"""Residency on a loopback llama-server (no /api/ps): loaded models count."""
from __future__ import annotations

import pytest

from src import background_job_guard as guard
from src import vram_admission


def _fake_get(responses):
    def fake(root, path, timeout):
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("404")
        return value
    return fake


def test_llama_server_single_model_is_resident(monkeypatch):
    monkeypatch.setattr(vram_admission, "_get", _fake_get({
        "/health": {"status": "ok"},
        "/v1/models": {"data": [{"id": "helper-3b"}]},
    }))
    names = guard._resident_model_names("http://127.0.0.1:8082/v1/chat/completions")
    assert names == ["helper-3b"]
    assert guard.would_require_load("http://127.0.0.1:8082/v1", "helper-3b") is False


def test_router_mode_counts_only_loaded(monkeypatch):
    monkeypatch.setattr(vram_admission, "_get", _fake_get({
        "/health": {"status": "ok"},
        "/v1/models": {"data": [{"id": "a", "status": {"value": "loaded"}},
                                 {"id": "b", "status": {"value": "unloaded"}}]},
    }))
    assert guard._resident_model_names("http://127.0.0.1:8081/v1") == ["a"]


@pytest.mark.parametrize("responses", [
    {"/health": {"status": "loading model"}, "/v1/models": {"data": [{"id": "a"}]}},
    {},
    {"/health": {"status": "ok"}, "/v1/models": {"data": []}},
])
def test_unknown_stays_unknown(monkeypatch, responses):
    monkeypatch.setattr(vram_admission, "_get", _fake_get(responses))
    assert guard._resident_model_names("http://127.0.0.1:8082/v1") is None


def test_remote_endpoint_is_never_probed(monkeypatch):
    called = []
    monkeypatch.setattr(vram_admission, "_get", lambda *a: called.append(a) or {})
    assert guard._resident_model_names("https://api.example.com/v1") is None
    assert called == []


def test_model_busy_reads_llama_server_slots(monkeypatch):
    monkeypatch.setattr(vram_admission, "_get", _fake_get({
        "/slots": [{"id": 0, "is_processing": True}],
    }))
    assert guard.model_busy("http://127.0.0.1:8082/v1") is True
    monkeypatch.setattr(vram_admission, "_get", _fake_get({
        "/slots": [{"id": 0, "is_processing": False}],
    }))
    assert guard.model_busy("http://127.0.0.1:8082/v1") is False
    assert guard.model_busy("https://api.example.com/v1") is False
