"""The opt-in wrapper reuses the real runner without silently calling a model."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts import benchmark_context_pair as bench
from src.bench import profiles, runner
from src.contracts.inference import InferenceProfile


@pytest.mark.parametrize("url", ["http://remote:11434", "http://127.0.0.1:8090/v1",
                                     "http://user:secret@localhost:11434",
                                     "http://localhost:11434/?token=secret",
                                     "http://localhost:11434/api/chat"])
def test_plan_rejects_unsupported_or_credential_bearing_endpoint(url):
    with pytest.raises(ValueError):
        bench.make_plan(url, "model")


def test_default_cli_is_plan_only_without_network(monkeypatch, tmp_path):
    def forbidden(*args):
        raise AssertionError("plan must not query or load a model")
    monkeypatch.setattr(bench, "_resident", forbidden)
    path = tmp_path / "plan.json"
    assert bench.main(["--endpoint", "http://127.0.0.1:11434/v1", "--model", "model",
                       "--out", str(path)]) == 0
    data = json.loads(path.read_text())
    assert data["state"] == "planned" and data["runs"] == []
    assert data["requested_contexts"] == [2048, 8192]


def test_no_resident_model_blocks_before_benchmark(monkeypatch, tmp_path):
    def unavailable(*args):
        raise ValueError("Requested model is not resident")
    monkeypatch.setattr(bench, "_resident", unavailable)
    monkeypatch.setattr(runner, "plan", lambda *a, **k: pytest.fail("must not plan a run"))
    report = asyncio.run(bench.execute(bench.make_plan("http://localhost:11434", "model"),
                                       str(tmp_path / "blocked.json")))
    assert report["state"] == "blocked_or_failed" and report["runs"] == []


@pytest.mark.parametrize("error", [None, "no answer"])
def test_runner_is_sequential_and_preserves_observed_vs_estimated(monkeypatch, tmp_path, error):
    base = InferenceProfile.parse({
        "id": "base", "label": "Test", "model": {"artifact_id": "model"},
        "engine": {"implementation": "ollama", "host": "localhost", "port": 11434,
                   "managed": "external"}, "objective": "interactive",
        "created_at": "2026-09-29T12:00:00Z", "source": "manual", "options": {},
    })
    monkeypatch.setattr(profiles, "current_profile", lambda *args: base)
    monkeypatch.setattr(bench, "_resident", lambda *args: {"model": "model", "context_length": None})
    calls = []
    def plan(profile, suite, budget, owner, **kwargs):
        context = profile.options["num_ctx"]
        assert profile.fingerprint != base.fingerprint
        calls.append(("plan", context))
        return SimpleNamespace(id=str(context), summary=SimpleNamespace(estimate_seconds=12.5))
    async def start(run_id):
        calls.append(("start", int(run_id)))
        return SimpleNamespace(state="completed", samples=[SimpleNamespace(error=error)],
                               to_dict=lambda: {"state": "completed", "summary": {
                                   "estimate_seconds": 12.5, "total_ms": 45.0},
                                   "samples": [{"error": error, "metrics": {"source": "observed_client"}}]})
    monkeypatch.setattr(runner, "plan", plan)
    monkeypatch.setattr(runner, "start", start)
    report = asyncio.run(bench.execute(bench.make_plan("http://localhost:11434", "model"),
                                       str(tmp_path / "run.json")))
    assert calls[:2] == [("plan", 2048), ("start", 2048)]
    assert report["heuristics"][0]["estimate_seconds"] == 12.5
    assert "estimate_seconds" not in report["runs"][0]["run"]["summary"]
    if error:
        assert len(calls) == 2 and report["state"] == "blocked_or_failed"
    else:
        assert calls[2:] == [("plan", 8192), ("start", 8192)]
        assert report["state"] == "completed"
        assert report["runs"][1]["resident_after"]["context_length"] is None
        assert report["contexts_verified"] is False


@pytest.mark.parametrize("context,expected", [(2048, 2048), (True, None),
                                              (-1, None), ("8192", None), (None, None)])
def test_resident_context_is_only_reported_when_valid(monkeypatch, context, expected):
    import httpx
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"models": [{"name": "model", "context_length": context}]}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    result = bench._resident("http://localhost:11434", "model")
    assert result["context_length"] == expected


@pytest.mark.parametrize("payload", [{"models": []}, {"models": "wrong"}, [],
                                      {"models": [{"name": "another-model"}]}])
def test_resident_metadata_does_not_accept_wrong_model_or_shape(monkeypatch, payload):
    import httpx
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(ValueError):
        bench._resident("http://localhost:11434", "model")
