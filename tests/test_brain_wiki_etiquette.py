"""`wiki.refresh_stale` is polite with the utility model.

Same rules as the extraction pass: no model call while a chat turn is in
flight (checked right before each call), and no call unless the model is
already resident, so an unattended summary refresh never loads or evicts a
model. A deferred entity keeps (or gets) deterministic bullets and is retried
on a later sweep.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402
from src.brain import wiki  # noqa: E402
from src import memory_engine as engine  # noqa: E402

OWNER = "alice"
MODEL = "utility-test:4b"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://127.0.0.1:11434/v1", MODEL, {}))
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _entity(name):
    ent = entities.upsert_entity(OWNER, name, type="person")
    item = engine.add_item(f"{name} works at Cordera Labs", owner=OWNER,
                           trust_class="human_explicit")
    entities.add_mention(OWNER, ent["id"], f"mem:{item['id']}")
    return ent, f"mem:{item['id'][:8]}"


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    async def _fake_call(*a, **k):
        seen.append(k.get("model"))
        prompt = k["messages"][0]["content"]
        ref = prompt.split("- [", 1)[1].split("]", 1)[0]
        return f"Works at Cordera Labs [{ref}]."

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    return seen


def _resident(monkeypatch, names):
    monkeypatch.setattr("src.background_job_guard._resident_model_names", lambda url: names)


@pytest.mark.asyncio
async def test_not_resident_defers_without_a_model_call(store, calls, monkeypatch):
    _resident(monkeypatch, ["other-model:70b"])
    ada, _ = _entity("Ada")
    report = await wiki.refresh_stale(OWNER)
    assert calls == []
    assert report["llm_skipped"] == "model_not_resident"
    assert report["deferred"] == 1
    got = entities.get_entity(ada["id"])
    assert "Ada" in got["summary"]           # deterministic bullets meanwhile
    assert got["facts_hash"] == ""           # still stale -> retried later


@pytest.mark.asyncio
async def test_deferred_entity_keeps_its_existing_summary(store, calls, monkeypatch):
    _resident(monkeypatch, None)
    ada, _ = _entity("Ada")
    entities.update_entity(ada["id"], summary="Older summary.")
    report = await wiki.refresh_stale(OWNER)
    assert calls == []
    assert report["llm_skipped"] == "residency_unknown"
    assert entities.get_entity(ada["id"])["summary"] == "Older summary."


@pytest.mark.asyncio
async def test_resident_model_writes_the_summary(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    ada, ref = _entity("Ada")
    report = await wiki.refresh_stale(OWNER)
    assert calls == [MODEL]
    assert report["updated"] == 1 and report["llm_skipped"] == ""
    assert ref in entities.get_entity(ada["id"])["summary"]


@pytest.mark.asyncio
async def test_interactive_turn_stops_the_sweep(store, calls, monkeypatch):
    _resident(monkeypatch, [MODEL])
    _entity("Ada")
    _entity("Bruno")
    state = {"n": 0}

    def _yield():
        state["n"] += 1
        return state["n"] > 1  # a turn starts right after the first summary

    monkeypatch.setattr("src.context_engine.maintenance.should_yield", _yield)
    report = await wiki.refresh_stale(OWNER, limit=5)
    assert calls == [MODEL]
    assert report["llm_skipped"] == "interactive_turn"
    assert report["updated"] == 1


@pytest.mark.asyncio
async def test_explicit_single_refresh_is_not_gated(store, calls, monkeypatch):
    # one entity refreshed because a person clicked it keeps its behaviour
    _resident(monkeypatch, None)
    ada, _ = _entity("Ada")
    result = await wiki.refresh_entity(ada["id"])
    assert calls == [MODEL]
    assert result["status"] == "updated"


@pytest.mark.asyncio
async def test_background_single_refresh_is_gated(store, calls, monkeypatch):
    _resident(monkeypatch, [])
    ada, _ = _entity("Ada")
    result = await wiki.refresh_entity(ada["id"], background=True)
    assert calls == []
    assert result["status"] == "deferred"
    assert result["reason"] == "model_not_resident"
