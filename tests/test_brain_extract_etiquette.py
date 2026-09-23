"""The background model pass of `extract_pending` is polite.

It never runs while a chat turn is in flight (checked right before EACH
model call, not once at task start) and never makes the runner load or evict
a model: the utility model is called only when it is already resident on its
endpoint. When the model pass is skipped the rule pass still runs, and the
report says why the model was not used.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import extract  # noqa: E402
from src import memory_engine as engine  # noqa: E402

OWNER = "alice"
MODEL = "utility-test:4b"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://127.0.0.1:11434/v1", MODEL, {}))
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    async def _fake_call(*a, **k):
        seen.append(k.get("model"))
        return json.dumps({"entities": [{"name": "Ada", "type": "person"}], "relations": []})

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    return seen


def _resident(monkeypatch, names):
    monkeypatch.setattr("src.background_job_guard._resident_model_names", lambda url: names)


def _settings(monkeypatch, **overrides):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: overrides.get(key, default))


@pytest.mark.asyncio
async def test_model_not_resident_skips_llm_but_rule_pass_runs(store, calls, monkeypatch):
    _resident(monkeypatch, ["some-other-model:70b"])
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == []
    assert report["processed"] == 1 and report["relations"] == 1
    assert report["llm_used"] is False
    assert report["llm_skipped"] == "model_not_resident"
    # nothing recorded for the model pass, so it is retried once the model is loaded
    item = engine.list_items(owner=OWNER)[0]
    assert extract._extraction_hash(OWNER, f"mem:{item['id']}", "llm") is None


@pytest.mark.asyncio
async def test_unknown_residency_skips_llm(store, calls, monkeypatch):
    _resident(monkeypatch, None)
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == []
    assert report["llm_skipped"] == "residency_unknown"
    assert report["processed"] == 1


@pytest.mark.asyncio
async def test_resident_model_is_used(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == [MODEL]
    assert report["llm_used"] is True
    assert report["llm_skipped"] == ""


@pytest.mark.asyncio
async def test_interactive_turn_blocks_the_model_call(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: True)
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == []
    assert report["llm_skipped"] == "interactive_turn"
    assert report["processed"] == 1  # the deterministic pass is not a model call


@pytest.mark.asyncio
async def test_yield_is_checked_before_every_batch(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    _settings(monkeypatch, brain_llm_extraction_batch=1)
    for text in ("Ada works at Cordera Labs", "Bruno lives in Villanueva"):
        engine.add_item(text, owner=OWNER, trust_class="human_explicit")
    state = {"n": 0}

    def _yield():
        state["n"] += 1
        return state["n"] > 1  # a turn starts right after the first model call

    monkeypatch.setattr("src.context_engine.maintenance.should_yield", _yield)
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == [MODEL]
    assert report["llm_used"] is True
    assert report["llm_skipped"] == "interactive_turn"


@pytest.mark.asyncio
async def test_owner_opt_in_lets_background_jobs_load_models(store, calls, monkeypatch):
    _resident(monkeypatch, [])
    _settings(monkeypatch, background_jobs_may_load_models=True)
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True)
    assert calls == [MODEL]
    assert report["llm_skipped"] == ""


@pytest.mark.asyncio
async def test_foreground_call_is_not_gated(store, calls, monkeypatch):
    _resident(monkeypatch, None)
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=True, background=False)
    assert calls == [MODEL]
    assert report["llm_skipped"] == ""


@pytest.mark.asyncio
async def test_llm_off_reports_no_skip_reason(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    engine.add_item("Ada works at Cordera Labs", owner=OWNER, trust_class="human_explicit")
    report = await extract.extract_pending(OWNER, use_llm=False)
    assert calls == []
    assert report["llm_skipped"] == ""


def test_gate_reasons(monkeypatch):
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)
    assert extract.background_llm_gate("", "") == "no_endpoint"
    _resident(monkeypatch, None)
    assert extract.background_llm_gate("http://x/v1", MODEL) == "residency_unknown"
    _resident(monkeypatch, [])
    assert extract.background_llm_gate("http://x/v1", MODEL) == "model_not_resident"
    _resident(monkeypatch, [MODEL.upper()])
    assert extract.background_llm_gate("http://x/v1", MODEL) == ""
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: True)
    assert extract.background_llm_gate("http://x/v1", MODEL) == "interactive_turn"
