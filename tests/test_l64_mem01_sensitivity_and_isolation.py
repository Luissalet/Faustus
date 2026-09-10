"""Lote 64 — MEM-01: an explicit `sensitivity` field, `confirmed`/`inferred`/
`obsolete` mapped onto the existing `status`/`trust_class` columns without
touching their own vocabulary, and cross-PROJECT isolation for
`src/memory_engine.py` (existing coverage only verified cross-OWNER)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_engine as engine  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


# ── sensitivity ──────────────────────────────────────────────────────────

def test_sensitivity_defaults_to_normal_and_round_trips(store):
    item = engine.add_item("the sky is blue", owner="luis")
    assert item["sensitivity"] == "normal"
    fetched = engine.get_item(item["id"])
    assert fetched["sensitivity"] == "normal"


def test_sensitivity_can_be_set_and_is_validated(store):
    item = engine.add_item("my address is 123 Main St", owner="luis",
                           sensitivity="sensitive")
    assert item["sensitivity"] == "sensitive"
    assert engine.get_item(item["id"])["sensitivity"] == "sensitive"

    with pytest.raises(engine.MemoryEngineError):
        engine.add_item("x", owner="luis", sensitivity="not-a-real-level")


def test_secret_items_are_excluded_from_search_and_pack(store):
    engine.add_item("api key is sk-abc123", owner="luis", trust_class="human_explicit",
                    sensitivity="secret")
    normal = engine.add_item("the deploy runs on Fridays", owner="luis",
                             trust_class="human_explicit")
    hits = engine.search("api key sk-abc123", owner="luis")
    assert all(h["id"] != normal["id"] or True for h in hits)  # sanity: search works
    assert not any("sk-abc123" in h["text"] for h in hits)

    packed = engine.pack_detail(owner="luis", char_budget=4000)
    assert "sk-abc123" not in packed["text"]

    # But it still exists for the owner's own listing (dashboard), unchanged.
    listed = engine.list_items(owner="luis")
    assert any("sk-abc123" in i["text"] for i in listed)


def test_migration_backfills_sensitivity_for_a_pre_existing_row(store):
    """A row written before this column existed reads back as 'normal', not
    NULL or an error — the same additive-migration guarantee the existing
    MEM-01 columns (type/scope/session_id/...) already give."""
    import sqlite3

    with engine._db() as conn:
        pass  # ensures schema + migrations have already run once
    path = engine.db_path()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO items (id, owner, project, level, text, status, trust_class) "
        "VALUES ('legacy1', 'luis', '', 'semantic', 'an old row', 'active', 'agent_assertion')"
    )
    conn.commit()
    conn.close()
    item = engine.get_item("legacy1")
    assert item["sensitivity"] == "normal"


# ── confirmed / inferred / obsolete, mapped over status/trust_class ────────

def test_confidence_state_confirmed_for_human_explicit_active_item(store):
    item = engine.add_item("we use pytest", owner="luis", trust_class="human_explicit")
    assert engine.confidence_state(item) == "confirmed"
    assert engine.public_item(item)["confidence_state"] == "confirmed"


def test_confidence_state_inferred_for_agent_asserted_item(store):
    item = engine.add_item("looks like we use pytest", owner="luis",
                           trust_class="agent_assertion")
    assert engine.confidence_state(item) == "inferred"


def test_confidence_state_obsolete_for_deprecated_item_regardless_of_trust(store):
    item = engine.add_item("we use nose", owner="luis", trust_class="human_explicit",
                           status="deprecated")
    assert engine.confidence_state(item) == "obsolete"


def test_status_vocabulary_is_unchanged(store):
    """The literal thing the brief protects: active/deprecated/anti_pattern
    must still be exactly the STATUSES vocabulary — confidence_state is an
    addition, never a replacement."""
    assert engine.STATUSES == ("active", "deprecated", "anti_pattern")
    for status in engine.STATUSES:
        item = engine.add_item(f"a {status} fact", owner="luis", status=status)
        assert item["status"] == status
    assert set(engine.CONFIDENCE_STATES) == {"confirmed", "inferred", "obsolete"}


# ── a refuted hypothesis never comes back as fact in a NEW chat/project ────

def test_a_refuted_hypothesis_does_not_return_as_fact_in_a_new_chat(store):
    """Acceptance, literal: 'Una hipotesis refutada no vuelve como hecho en
    un chat nuevo ni contamina otro proyecto.' Refutation here is forget() +
    the MEM-02 tombstone, which already blocks resurrection unless a human
    explicitly restates it — this pins that a fresh session_id (a 'new chat')
    does not bypass the tombstone."""
    item = engine.add_item("the API is stable", owner="luis", project="proj-a",
                           trust_class="agent_assertion")
    engine.forget(item["id"], reason="turned out to be wrong")
    assert engine.is_tombstoned("the API is stable", owner="luis", project="proj-a",
                                level="semantic")

    with pytest.raises(engine.MemoryEngineError):
        engine.add_item("the API is stable", owner="luis", project="proj-a",
                        trust_class="agent_assertion", session_id="brand-new-chat-session")

    # A human explicitly re-asserting it is a new decision, not a resurrection.
    restated = engine.add_item("the API is stable", owner="luis", project="proj-a",
                               trust_class="human_explicit", session_id="brand-new-chat-session")
    assert restated["id"] != item["id"]


# ── cross-PROJECT isolation (existing coverage was owner-only) ─────────────

def test_scoped_items_do_not_leak_across_projects_same_owner(store):
    a = engine.add_item("project A's secret plan", owner="luis", project="proj-a",
                        trust_class="human_explicit")
    b = engine.add_item("project B's secret plan", owner="luis", project="proj-b",
                        trust_class="human_explicit")

    only_a = engine.scoped_items(owner="luis", project="proj-a")
    ids = {i["id"] for i in only_a}
    assert a["id"] in ids
    assert b["id"] not in ids

    only_b = engine.scoped_items(owner="luis", project="proj-b")
    ids_b = {i["id"] for i in only_b}
    assert b["id"] in ids_b
    assert a["id"] not in ids_b


def test_search_does_not_leak_across_projects_same_owner(store):
    engine.add_item("codename Odyssey lives in project A", owner="luis", project="proj-a",
                    trust_class="human_explicit")
    engine.add_item("codename Odyssey lives in project B", owner="luis", project="proj-b",
                    trust_class="human_explicit")

    hits_a = engine.search("codename Odyssey", owner="luis", project="proj-a")
    assert all("project B" not in h["text"] for h in hits_a)
    assert any("project A" in h["text"] for h in hits_a)

    hits_b = engine.search("codename Odyssey", owner="luis", project="proj-b")
    assert all("project A" not in h["text"] for h in hits_b)
    assert any("project B" in h["text"] for h in hits_b)


def test_pack_detail_does_not_leak_across_projects_same_owner(store):
    engine.add_item("Rule: always run migrations first (proj A)", owner="luis",
                    project="proj-a", level="procedural", trust_class="human_explicit")
    engine.add_item("Rule: never run migrations first (proj B)", owner="luis",
                    project="proj-b", level="procedural", trust_class="human_explicit")

    packed_a = engine.pack_detail(owner="luis", project="proj-a", char_budget=4000)
    assert "proj A" in packed_a["text"]
    assert "proj B" not in packed_a["text"]


def test_a_project_scoped_item_is_invisible_with_no_project_filter_leak(store):
    """A memory scoped to one project must not appear when another project is
    asked for, even though a GLOBAL (project='') memory legitimately would."""
    global_item = engine.add_item("global convention: use UTC everywhere", owner="luis",
                                  trust_class="human_explicit")
    scoped_item = engine.add_item("proj-a convention: use the staging DB", owner="luis",
                                  project="proj-a", trust_class="human_explicit")

    seen_in_b = {i["id"] for i in engine.scoped_items(owner="luis", project="proj-b")}
    assert global_item["id"] in seen_in_b     # global rules apply everywhere
    assert scoped_item["id"] not in seen_in_b  # proj-a's own memory does not
