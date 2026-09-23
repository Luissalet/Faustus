"""Tests for src/brain/extract.py — deterministic + optional LLM extraction."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402
from src.brain import extract  # noqa: E402
from src import memory_engine as engine  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


@pytest.fixture(autouse=True)
def model_resident_and_idle(monkeypatch):
    """The background model pass only runs when no turn is in flight and the
    utility model is already loaded (see test_brain_extract_etiquette.py);
    these tests are about what it does once it runs."""
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)
    monkeypatch.setattr("src.background_job_guard._resident_model_names",
                        lambda url: ["qwen-test"])


# ---------------------------------------------------------------------------
# extract_source — deterministic
# ---------------------------------------------------------------------------


def test_extract_source_finds_known_entity_and_new_proper_noun(store):
    entities.upsert_entity("alice", "Ada", type="person")
    result = extract.extract_source("alice", "mem:1", "Ada works at Cordera Labs")
    names = {e["name"] for e in result["entities"]}
    assert "Ada" in names
    assert "Cordera Labs" in names
    assert len(result["relations"]) == 1
    rel = result["relations"][0]
    assert rel["rel"] == "works_at"

    ada = entities.upsert_entity("alice", "Ada")  # resolves to the same entity
    assert "mem:1" in entities.sources_for(ada["id"])


def test_extract_source_is_grounded_only_to_multiword_proper_nouns(store):
    result = extract.extract_source("alice", "mem:2", "Bruno said hello to everyone")
    # "Bruno" alone is a single-word capitalised name: the deterministic
    # pass never invents a brand-new single-word entity from thin air.
    names = {e["name"] for e in result["entities"]}
    assert "Bruno" not in names


def test_extract_source_attaches_temporal_window_to_relations(store):
    result = extract.extract_source(
        "alice", "mem:3", "Ada trabaja en Cordera Labs desde marzo de 2025")
    assert len(result["relations"]) == 1
    assert result["relations"][0]["valid_from"] == "2025-03-01T00:00:00Z"


def test_extract_source_self_reference(store):
    result = extract.extract_source("alice", "mem:4", "I use Python every day")
    rel = result["relations"][0]
    self_ent = entities.self_entity("alice")
    assert rel["src"] == self_ent["id"]
    assert rel["rel"] == "uses"


def test_extract_source_records_extraction_log(store):
    extract.extract_source("alice", "mem:5", "Ada works at Cordera Labs")
    assert extract._extraction_hash("alice", "mem:5", "rule") is not None


def test_extract_source_empty_inputs_are_safe(store):
    assert extract.extract_source("", "mem:1", "text") == {"entities": [], "relations": []}
    assert extract.extract_source("alice", "", "text") == {"entities": [], "relations": []}
    assert extract.extract_source("alice", "mem:1", "") == {"entities": [], "relations": []}


# ---------------------------------------------------------------------------
# extract_pending — rule pass, incremental, budget/limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_pending_processes_memory_items_and_is_incremental(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    engine.add_item("Ada works at Cordera Labs", owner="alice", trust_class="human_explicit")

    report = await extract.extract_pending("alice", use_llm=False)
    assert report["processed"] == 1
    assert report["entities"] >= 1
    assert report["relations"] == 1
    assert report["llm_used"] is False

    again = await extract.extract_pending("alice", use_llm=False)
    assert again["processed"] == 0
    assert again["skipped"] == 1


@pytest.mark.asyncio
async def test_extract_pending_includes_personal_memories(store, monkeypatch):
    from src.memory import MemoryManager

    (store / "engine").mkdir(parents=True, exist_ok=True)
    mm = MemoryManager(str(store / "engine"))
    entry = mm.add_entry("Bruno vive en Madrid", source="user", category="fact", owner="alice")
    mm.save([entry])

    report = await extract.extract_pending("alice", use_llm=False)
    assert report["processed"] == 1


@pytest.mark.asyncio
async def test_extract_pending_respects_limit(store):
    for i in range(3):
        engine.add_item(f"Fact number {i} about the project", owner="alice",
                        trust_class="human_explicit")
    report = await extract.extract_pending("alice", limit=1, use_llm=False)
    assert report["processed"] == 1


@pytest.mark.asyncio
async def test_extract_pending_respects_budget(store, monkeypatch):
    for i in range(5):
        engine.add_item(f"Fact number {i} about the project", owner="alice",
                        trust_class="human_explicit")

    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1000.0

    monkeypatch.setattr(extract.time, "monotonic", _fake_monotonic)
    report = await extract.extract_pending("alice", budget_s=5.0, use_llm=False)
    assert report["processed"] == 0


@pytest.mark.asyncio
async def test_extract_pending_disabled_setting_is_a_noop(store, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: False if key == "brain_entity_extraction" else default)
    engine.add_item("Ada works at Cordera Labs", owner="alice", trust_class="human_explicit")
    report = await extract.extract_pending("alice")
    assert report["processed"] == 0
    assert report["entities"] == 0


@pytest.mark.asyncio
async def test_extract_pending_never_raises_when_extract_source_breaks(store, monkeypatch):
    engine.add_item("Ada works at Cordera Labs", owner="alice", trust_class="human_explicit")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract, "extract_source", _boom)
    report = await extract.extract_pending("alice", use_llm=False)
    assert report["errors"] == 1
    assert report["processed"] == 0


@pytest.mark.asyncio
async def test_extract_pending_no_owner_is_a_noop(store):
    report = await extract.extract_pending("")
    assert report["processed"] == 0


# ---------------------------------------------------------------------------
# extract_pending — LLM pass, fake model, grounding filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_pending_llm_pass_keeps_only_grounded_names(store, monkeypatch):
    engine.add_item("Ada mentioned meeting Grace at the office", owner="alice",
                    trust_class="human_explicit")

    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )

    reply = json.dumps({
        "entities": [
            {"name": "Ada", "type": "person", "aliases": []},
            {"name": "Grace", "type": "person", "aliases": []},
            {"name": "Henry Ford", "type": "person", "aliases": []},  # never in the text
        ],
        "relations": [
            {"src": "Ada", "rel": "knows", "dst": "Grace", "valid_from": None, "valid_until": None},
            {"src": "Ada", "rel": "knows", "dst": "Henry Ford", "valid_from": None, "valid_until": None},
        ],
    })

    async def _fake_call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)

    report = await extract.extract_pending("alice", use_llm=True)
    assert report["llm_used"] is True
    assert report["entities"] == 2  # Ada and Grace, not the hallucinated Henry Ford

    names = {e["name"] for e in entities.list_entities("alice")}
    assert "Ada" in names and "Grace" in names
    assert "Henry Ford" not in names

    ada = entities.upsert_entity("alice", "Ada")
    grace = entities.upsert_entity("alice", "Grace")
    relations = entities.list_relations("alice", entity_id=ada["id"])
    assert any(r["dst"] == grace["id"] and r["rel"] == "knows" for r in relations)
    # the relation to the hallucinated entity was never stored
    assert all(r["dst"] != "" or r["dst_value"] != "Henry Ford" for r in relations)


@pytest.mark.asyncio
async def test_extract_pending_llm_pass_keeps_grounded_literal_destination(store, monkeypatch):
    engine.add_item("Ada uses Python for everything", owner="alice", trust_class="human_explicit")
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )
    reply = json.dumps({
        "entities": [{"name": "Ada", "type": "person", "aliases": []}],
        "relations": [
            {"src": "Ada", "rel": "uses", "dst": "Python", "valid_from": None, "valid_until": None},
            {"src": "Ada", "rel": "uses", "dst": "Cobol", "valid_from": None, "valid_until": None},
        ],
    })

    async def _fake_call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    report = await extract.extract_pending("alice", use_llm=True)
    ada = entities.upsert_entity("alice", "Ada")
    relations = entities.list_relations("alice", entity_id=ada["id"])
    dst_values = {r["dst_value"] for r in relations}
    assert "Python" in dst_values
    assert "Cobol" not in dst_values  # not present in the source text, dropped


@pytest.mark.asyncio
async def test_extract_pending_llm_call_failure_is_never_raised(store, monkeypatch):
    engine.add_item("Ada works at Cordera Labs", owner="alice", trust_class="human_explicit")
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )

    async def _boom(*a, **k):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr("src.llm_core.llm_call_async", _boom)
    report = await extract.extract_pending("alice", use_llm=True)
    assert report["llm_used"] is False
    assert report["errors"] >= 1


@pytest.mark.asyncio
async def test_extract_pending_no_endpoint_runs_rule_only(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    engine.add_item("Ada works at Cordera Labs", owner="alice", trust_class="human_explicit")
    report = await extract.extract_pending("alice", use_llm=True)
    assert report["llm_used"] is False
    assert report["processed"] == 1
