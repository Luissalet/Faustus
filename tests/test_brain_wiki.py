"""Tests for src/brain/wiki.py — deterministic fallback + cited LLM summary."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402
from src.brain import wiki  # noqa: E402
from src import memory_engine as engine  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _make_ada_with_facts(owner="alice"):
    ada = entities.upsert_entity(owner, "Ada", type="person")
    labs = entities.upsert_entity(owner, "Cordera Labs", type="organization")
    item = engine.add_item("Ada works at Cordera Labs", owner=owner, trust_class="human_explicit")
    entities.add_mention(owner, ada["id"], f"mem:{item['id']}")
    entities.add_relation(owner, ada["id"], "works_at", dst_id=labs["id"])
    return ada


# ---------------------------------------------------------------------------
# render_summary_fallback
# ---------------------------------------------------------------------------


def test_render_summary_fallback_includes_name_relation_and_fact(store):
    ada = _make_ada_with_facts()
    prof = entities.profile(ada["id"])
    text = wiki.render_summary_fallback(prof)
    assert "Ada" in text
    assert "works_at" in text
    assert "Cordera Labs" in text


def test_render_summary_fallback_never_crashes_on_empty_profile(store):
    ent = entities.upsert_entity("alice", "Ghost")
    prof = entities.profile(ent["id"])
    text = wiki.render_summary_fallback(prof)
    assert "Ghost" in text


# ---------------------------------------------------------------------------
# refresh_entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_entity_not_found(store):
    result = await wiki.refresh_entity("nope")
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_refresh_entity_locked_is_skipped(store):
    ada = _make_ada_with_facts()
    entities.update_entity(ada["id"], summary="hand-written", summary_locked=True)
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "locked"
    assert entities.get_entity(ada["id"])["summary"] == "hand-written"


@pytest.mark.asyncio
async def test_refresh_entity_locked_can_be_forced(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    ada = _make_ada_with_facts()
    entities.update_entity(ada["id"], summary="hand-written", summary_locked=True)
    result = await wiki.refresh_entity(ada["id"], force=True)
    assert result["status"] == "fallback"


@pytest.mark.asyncio
async def test_refresh_entity_no_endpoint_uses_fallback(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    ada = _make_ada_with_facts()
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "fallback"
    stored = entities.get_entity(ada["id"])
    assert "Ada" in stored["summary"]
    assert stored["facts_hash"]


@pytest.mark.asyncio
async def test_refresh_entity_unchanged_facts_are_skipped(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    ada = _make_ada_with_facts()
    first = await wiki.refresh_entity(ada["id"])
    assert first["status"] == "fallback"
    second = await wiki.refresh_entity(ada["id"])
    assert second["status"] == "unchanged"


@pytest.mark.asyncio
async def test_refresh_entity_new_facts_invalidate_the_cache(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    ada = _make_ada_with_facts()
    await wiki.refresh_entity(ada["id"])
    item2 = engine.add_item("Ada also uses Python", owner="alice", trust_class="human_explicit")
    entities.add_mention("alice", ada["id"], f"mem:{item2['id']}")
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "fallback"  # re-rendered, not skipped


@pytest.mark.asyncio
async def test_refresh_entity_disabled_setting(store, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "brain_wiki_summaries" else default,
    )
    ada = _make_ada_with_facts()
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_refresh_entity_accepts_grounded_llm_summary(store, monkeypatch):
    ada = _make_ada_with_facts()
    prof = entities.profile(ada["id"])
    ref = wiki._fact_refs(prof)[0]

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "qwen-test", {}))

    async def _fake_call(*a, **k):
        return f"Ada works at Cordera Labs [{ref}]."

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "updated"
    stored = entities.get_entity(ada["id"])
    assert ref in stored["summary"]
    assert stored["summary_sources"] == [ref]


@pytest.mark.asyncio
async def test_refresh_entity_rejects_unknown_citation(store, monkeypatch):
    ada = _make_ada_with_facts()
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "qwen-test", {}))

    async def _fake_call(*a, **k):
        return "Ada works at Cordera Labs [mem:deadbeef]."  # not a real fact ref

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "fallback"
    stored = entities.get_entity(ada["id"])
    assert "mem:deadbeef" not in stored["summary"]


@pytest.mark.asyncio
async def test_refresh_entity_rejects_uncited_summary_when_facts_exist(store, monkeypatch):
    ada = _make_ada_with_facts()
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "qwen-test", {}))

    async def _fake_call(*a, **k):
        return "Ada works at Cordera Labs."  # no citation at all

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "fallback"


@pytest.mark.asyncio
async def test_refresh_entity_model_failure_falls_back(store, monkeypatch):
    ada = _make_ada_with_facts()
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "qwen-test", {}))

    async def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("src.llm_core.llm_call_async", _boom)
    result = await wiki.refresh_entity(ada["id"])
    assert result["status"] == "fallback"


@pytest.mark.asyncio
async def test_refresh_entity_accepts_summary_with_no_citations_when_no_facts(store, monkeypatch):
    ghost = entities.upsert_entity("alice", "Ghost")
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "qwen-test", {}))

    async def _fake_call(*a, **k):
        return "Nothing is known about Ghost yet."

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    result = await wiki.refresh_entity(ghost["id"])
    assert result["status"] == "updated"


# ---------------------------------------------------------------------------
# refresh_stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_stale_sweeps_and_skips_locked(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    ada = _make_ada_with_facts("alice")
    locked = entities.upsert_entity("alice", "Locked One")
    entities.update_entity(locked["id"], summary="fixed", summary_locked=True)

    report = await wiki.refresh_stale("alice", limit=10)
    assert report["updated"] >= 1
    assert entities.get_entity(ada["id"])["summary"]
    assert entities.get_entity(locked["id"])["summary"] == "fixed"


@pytest.mark.asyncio
async def test_refresh_stale_respects_limit(store, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: (None, None, None))
    for i in range(3):
        entities.upsert_entity("alice", f"Entity {i}")
    report = await wiki.refresh_stale("alice", limit=1)
    assert report["checked"] == 1
