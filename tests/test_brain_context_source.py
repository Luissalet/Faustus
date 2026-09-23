"""tests/test_brain_context_source.py — BrainSource, the second brain as a
retrieval source (src/context_engine/adapters/brain.py).

Pins the same things `test_context_engine_sources.py` pins for the memory
adapters: the gate (policy + settings), the two candidate kinds (entity card,
note hit), by-id `fetch`, and owner isolation. Isolation is a real, tmp-dir
brain store — cheaper to set up correctly than faking `src.brain.entities`
and `src.brain.notes` separately.

Also pins that the source is actually WIRED: registered in
`adapters.SOURCE_FACTORIES` and declared in `planner.SOURCE_SECTIONS` /
`PERSONAL_SOURCE_IDS` — the failure mode a prior integration hit was
"constructed but never registered", invisible to a unit test that only
imports the class directly.
"""

from __future__ import annotations

import asyncio

import pytest

from src.brain import db as brain_db
from src.context_engine import candidates as C
from src.context_engine import planner
from src.context_engine.adapters.brain import BrainSource
from src.context_engine.contracts import (
    ContextExecution,
    ContextPolicy,
    ContextRequest,
    ContextTask,
)

OWNER = "ada"


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    yield
    brain_db.use_dir(None)


def _request(*, owner=OWNER, query="", **policy_kwargs) -> C.RetrievalRequest:
    return C.RetrievalRequest(
        request=ContextRequest(
            execution=ContextExecution(owner=owner),
            task=ContextTask(query=query),
            policy=ContextPolicy(**policy_kwargs),
        ),
        query=query,
    )


def _search(source: BrainSource, req: C.RetrievalRequest):
    return asyncio.run(source.search(req))


def _fetch(source: BrainSource, ref: str, req: C.RetrievalRequest):
    return asyncio.run(source.fetch(ref, req))


# ── wiring ───────────────────────────────────────────────────────────────

def test_the_source_is_registered_and_declared():
    C.reset_sources()
    C.default_sources()
    assert "brain" in C.registered_sources()
    assert "brain" in planner.known_sources()
    assert planner.SOURCE_SECTIONS["brain"] == ("retrieved_memory",)
    assert "brain" in planner.PERSONAL_SOURCE_IDS


# ── gating ───────────────────────────────────────────────────────────────

def test_gate_refuses_without_personal_memory(monkeypatch):
    from src.brain import entities

    entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    source = BrainSource()
    req = _request(query="Bruno Villanueva", allow_personal_memory=False)
    assert _search(source, req) == ()


def test_gate_refuses_when_section_not_wanted():
    source = BrainSource()
    req = C.RetrievalRequest(
        request=ContextRequest(execution=ContextExecution(owner=OWNER),
                               task=ContextTask(query="x")),
        query="x", sections=("active_goal",),
    )
    assert _search(source, req) == ()


def test_gate_refuses_when_brain_disabled(monkeypatch):
    from src import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: False if key == "brain_enabled" else default,
    )
    source = BrainSource()
    assert _search(source, _request(query="anything")) == ()


def test_gate_refuses_when_context_source_off(monkeypatch):
    from src import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: True if key == "brain_enabled"
        else (False if key == "brain_context_source" else default),
    )
    source = BrainSource()
    assert _search(source, _request(query="anything")) == ()


def test_no_query_produces_nothing():
    source = BrainSource()
    assert _search(source, _request(query="")) == ()


# ── entity cards ─────────────────────────────────────────────────────────

def test_entity_mentioned_in_the_query_becomes_a_candidate():
    from src.brain import entities

    entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    source = BrainSource()
    results = _search(source, _request(query="what do I know about Bruno Villanueva"))
    ent = [c for c in results if c.source_ref.startswith("ent:")]
    assert len(ent) == 1
    assert "Bruno Villanueva" in ent[0].title
    assert ent[0].trust_class == "agent_assertion"
    assert ent[0].authority == "agent_claim"


def test_entity_card_includes_summary_and_facts():
    from src.brain import entities

    entity = entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    entities.update_entity(entity["id"], summary="A small design studio.")
    source = BrainSource()
    results = _search(source, _request(query="tell me about Cordera Labs"))
    ent = [c for c in results if c.source_ref == f"ent:{entity['id']}"]
    assert len(ent) == 1
    assert "design studio" in ent[0].body


def test_unmentioned_entity_is_not_returned():
    from src.brain import entities

    entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    source = BrainSource()
    results = _search(source, _request(query="what is the weather like today"))
    assert not [c for c in results if c.source_ref.startswith("ent:")]


# ── note hits ────────────────────────────────────────────────────────────

def test_note_hit_becomes_a_candidate():
    from src.brain import notes

    notes.create_note(OWNER, "Coffee ideas", content="Try a Kalita pour-over.")
    source = BrainSource()
    results = _search(source, _request(query="Kalita"))
    note_hits = [c for c in results if c.source_ref.startswith("note:")]
    assert len(note_hits) == 1
    assert note_hits[0].trust_class == "observed"
    assert note_hits[0].authority == "observed_state"


def test_note_and_entity_hits_can_both_appear():
    from src.brain import entities, notes

    entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    notes.create_note(OWNER, "Roadmap", content="Meeting with Cordera Labs about the roadmap.")
    source = BrainSource()
    results = _search(source, _request(query="Cordera Labs roadmap"))
    kinds = {c.source_ref.split(":")[0] for c in results}
    assert "ent" in kinds
    assert "note" in kinds


# ── fetch (by id) ────────────────────────────────────────────────────────

def test_fetch_entity_by_ref():
    from src.brain import entities

    entity = entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    source = BrainSource()
    candidate = _fetch(source, f"ent:{entity['id']}", _request())
    assert candidate is not None
    assert "Bruno Villanueva" in candidate.title


def test_fetch_note_by_ref():
    from src.brain import notes

    note = notes.create_note(OWNER, "Log", content="First entry.")
    source = BrainSource()
    candidate = _fetch(source, f"note:{note['path']}", _request())
    assert candidate is not None
    assert "First entry." in candidate.body


def test_fetch_unknown_ref_is_none():
    source = BrainSource()
    assert _fetch(source, "ent:nope", _request()) is None
    assert _fetch(source, "note:Notes/nope.md", _request()) is None
    assert _fetch(source, "mem:1", _request()) is None


def test_fetch_refuses_another_owners_entity():
    from src.brain import entities

    entity = entities.upsert_entity("mallory", "Secret Project", type="project")
    source = BrainSource()
    candidate = _fetch(source, f"ent:{entity['id']}", _request(owner=OWNER))
    assert candidate is None


def test_search_does_not_cross_owners():
    from src.brain import entities

    entities.upsert_entity("mallory", "Only Mallory Knows", type="person")
    source = BrainSource()
    results = _search(source, _request(owner=OWNER, query="Only Mallory Knows"))
    assert not [c for c in results if c.source_ref.startswith("ent:")]
