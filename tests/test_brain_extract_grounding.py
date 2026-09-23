"""Regression tests for the model-extraction grounding bypass (review 1,
finding 10).

The model sees a BATCH of sources; grounding used to be checked against
the whole batch's text, and its dates were trusted. So "Ada" from one
source and "Bluehaven" from another became "Ada works at Bluehaven since
2020" — citing every source of the batch — and closed the relation read
deterministically from the text. Now both ends must occur in the SAME
source, only that source is cited, a model date survives only when that
source's own text supports it, and a model relation never closes a
rule-derived one.
"""

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

OWNER = "alice"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "local-test", {}))
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _model_replies(monkeypatch, payload):
    async def _fake_call(*a, **k):
        return json.dumps(payload)

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)


def _report():
    return {"entities": 0, "relations": 0, "errors": 0}


def _rels(entity_id):
    return entities.list_relations(OWNER, entity_id=entity_id)


@pytest.mark.asyncio
async def test_cross_source_relation_is_dropped_and_rule_relation_survives(store, monkeypatch):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    cord = entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    rule = entities.add_relation(OWNER, ada["id"], "works_at", dst_id=cord["id"],
                                 valid_from="2022-01-01")
    batch = [{"source_ref": "mem:1", "text": "Ada works at Cordera Labs."},
             {"source_ref": "mem:2", "text": "Bruno visited Bluehaven last week."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}, {"name": "Bluehaven", "type": "organization"}],
        "relations": [{"src": "Ada", "rel": "works at", "dst": "Bluehaven",
                       "valid_from": "2020-01-01"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())

    rels = _rels(ada["id"])
    assert [r["id"] for r in rels] == [rule["id"]]
    assert rels[0]["status"] == "active" and rels[0]["valid_until"] == ""


@pytest.mark.asyncio
async def test_same_source_relation_cites_only_that_source(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada knows Grace from the office."},
             {"source_ref": "mem:2", "text": "Grace likes tea."},
             {"source_ref": "mem:3", "text": "Ada plays chess."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}, {"name": "Grace", "type": "person"}],
        "relations": [{"src": "Ada", "rel": "knows", "dst": "Grace"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    rels = _rels(ada["id"])
    assert len(rels) == 1
    assert rels[0]["evidence"] == ["mem:1"]
    assert rels[0]["method"] == "llm"


@pytest.mark.asyncio
async def test_literal_destination_must_occur_in_the_same_source(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada writes code every day."},
             {"source_ref": "mem:2", "text": "The team prefers Python."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}],
        "relations": [{"src": "Ada", "rel": "uses", "dst": "Python"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    assert _rels(ada["id"]) == []


@pytest.mark.asyncio
async def test_model_dates_not_in_the_source_text_are_dropped(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada works at Bluehaven."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}, {"name": "Bluehaven", "type": "organization"}],
        "relations": [{"src": "Ada", "rel": "works_at", "dst": "Bluehaven",
                       "valid_from": "2020-01-01", "valid_until": "2021-06-30"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    rels = _rels(ada["id"])
    assert len(rels) == 1
    assert not rels[0]["valid_from"].startswith("2020")
    assert rels[0]["valid_until"] == ""


@pytest.mark.asyncio
async def test_model_dates_supported_by_the_source_text_are_kept(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada works at Bluehaven since March 2021."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}, {"name": "Bluehaven", "type": "organization"}],
        "relations": [{"src": "Ada", "rel": "works_at", "dst": "Bluehaven",
                       "valid_from": "2021-03-01", "valid_until": "2023-01-01"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    rels = _rels(ada["id"])
    assert len(rels) == 1
    assert rels[0]["valid_from"] == "2021-03-01T00:00:00Z"
    assert rels[0]["valid_until"] == ""  # "2023" is nowhere in the text


@pytest.mark.asyncio
async def test_model_relation_never_closes_a_rule_relation(store, monkeypatch):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    cord = entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    rule = entities.add_relation(OWNER, ada["id"], "works_at", dst_id=cord["id"],
                                 valid_from="2022-01-01")
    batch = [{"source_ref": "mem:9", "text": "Ada works at Bluehaven since 2024."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}, {"name": "Bluehaven", "type": "organization"}],
        "relations": [{"src": "Ada", "rel": "works_at", "dst": "Bluehaven",
                       "valid_from": "2024-01-01"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    kept = next(r for r in _rels(ada["id"]) if r["id"] == rule["id"])
    assert kept["status"] == "active" and kept["valid_until"] == ""


def test_rule_pass_resolves_bare_months_relative_to_the_source_date(store):
    # Written in October 2025: "until March" is March 2026, whenever the
    # extraction happens to run.
    result = extract.extract_source(OWNER, "mem:x", "Ada works at Bluehaven until March",
                                    created_at="2025-10-15T09:00:00Z")
    rels = result["relations"]
    assert rels and rels[0]["valid_until"] == "2026-03-31T23:59:59Z"
