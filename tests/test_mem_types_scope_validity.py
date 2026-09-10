"""MEM-01 — tipos, alcance, vigencia y procedencia (src/memory_engine.py).

Additive migration: `type` (preference|fact|procedure|decision|anti_pattern),
`scope` (global|project:<id>|session:<id>), `valid_from`/`valid_until` and
`provenance` are new columns on the existing `items` table, backfilled from
the fields that already carried the same information — no existing row, and
no existing caller that never heard of these kwargs, loses anything.

Before this change none of these fields existed (see
docs/spec/v2/MAPA_REUTILIZACION.md, MEM-01 row: "no hay campo de
'sensibilidad' explícito ... no se verificó aislamiento cruzado por
proyecto"). Reverting `src/memory_engine.py`'s MEM-01 migration/kwargs makes
every test below fail: either with a KeyError/AttributeError building the
item, or — for `test_a_hypothesis_with_an_expired_validity_window_...` — by
the refuted fact coming back in `search`/`pack_detail` because the store has
no `valid_until` to check.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_engine as engine  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


def test_type_defaults_from_level_and_status_when_not_given(store):
    fact = engine.add_item("The cart total excludes shipping", owner="luis", now=NOW)
    assert fact["type"] == "fact"

    rule = engine.add_item("Always run tests before claiming done", owner="luis",
                           level="procedural", trust_class="human_explicit", now=NOW)
    assert rule["type"] == "procedure"

    anti = engine.add_item("Never skip the tests", owner="luis", status="anti_pattern",
                           trust_class="human_explicit", now=NOW)
    assert anti["type"] == "anti_pattern"


def test_type_is_validated_against_the_known_vocabulary(store):
    item = engine.add_item("Ship on Fridays", owner="luis", type="decision", now=NOW)
    assert item["type"] == "decision"

    with pytest.raises(engine.MemoryEngineError):
        engine.add_item("bad type", owner="luis", type="opinion", now=NOW)


def test_scope_is_computed_from_owner_project_and_session(store):
    global_item = engine.add_item("A global preference", owner="luis", now=NOW)
    assert global_item["scope"] == "global"

    project_item = engine.add_item("A project fact", owner="luis", project="acme", now=NOW)
    assert project_item["scope"] == "project:acme"

    session_item = engine.add_item("A one-off note", owner="luis", project="acme",
                                   session_id="sess-1", now=NOW)
    assert session_item["scope"] == "session:sess-1"


def test_provenance_records_who_said_it(store):
    item = engine.add_item(
        "The deploy window is 2am UTC", owner="luis", trust_class="human_explicit",
        provenance={"turn": "t-42", "message_id": "m-7"}, now=NOW,
    )
    assert item["provenance"]["turn"] == "t-42"
    assert item["provenance"]["message_id"] == "m-7"
    assert item["provenance"]["who"] == "human_explicit"

    reloaded = engine.get_item(item["id"])
    assert reloaded["provenance"]["turn"] == "t-42"   # survives the JSON round trip


def test_a_hypothesis_with_an_expired_validity_window_does_not_come_back(store):
    """MEM-01 acceptance: "una hipótesis refutada no vuelve como hecho"."""
    item = engine.add_item(
        "The bug is caused by a race condition", owner="luis", project="acme",
        trust_class="agent_assertion", valid_until=engine._iso(NOW - timedelta(days=1)),
        now=NOW - timedelta(days=5),
    )
    hits = engine.search("race condition", "luis", "acme", now=NOW)
    assert all(h["id"] != item["id"] for h in hits)

    detail = engine.pack_detail("luis", "acme", "race condition", 2000, now=NOW)
    assert item["id"] not in detail["ids"]

    # The row itself is untouched (an expired item is not deleted, only not
    # recalled) — the dashboard can still show and re-validate it by hand.
    assert engine.get_item(item["id"]) is not None


def test_a_not_yet_valid_item_is_not_recalled_before_its_start(store):
    item = engine.add_item(
        "New pricing takes effect", owner="luis", project="acme",
        trust_class="human_explicit", valid_from=engine._iso(NOW + timedelta(days=30)),
        now=NOW,
    )
    hits = engine.search("New pricing", "luis", "acme", now=NOW)
    assert all(h["id"] != item["id"] for h in hits)


def test_an_item_with_no_validity_window_is_always_recallable(store):
    """The default for every item added before this field existed."""
    item = engine.add_item("Coffee before code", owner="luis", now=NOW)
    hits = engine.search("Coffee before code", "luis", "", now=NOW + timedelta(days=3650))
    assert any(h["id"] == item["id"] for h in hits)


def test_migration_is_additive_over_a_pre_mem01_database(store, tmp_path):
    """Simulate a store created before MEM-01: build the OLD schema by hand
    (no type/scope/valid_from/valid_until/provenance columns), insert a row
    the old way, then open it through the engine and confirm every old row
    is still there with sane backfilled defaults — no data lost."""
    path = engine.db_path()
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE items (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT 'semantic',
            text TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            trust_class TEXT NOT NULL DEFAULT 'agent_assertion', trust REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'active',
            maturity TEXT NOT NULL DEFAULT 'candidate', evidence TEXT NOT NULL DEFAULT '[]',
            helpful TEXT NOT NULL DEFAULT '[]', harmful TEXT NOT NULL DEFAULT '[]',
            inverted_from TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '', last_accessed TEXT NOT NULL DEFAULT '',
            access_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO items (id, owner, project, level, text, status, maturity, "
        "trust_class, trust, confidence, created_at, updated_at) VALUES "
        "('pre-mem01', 'luis', 'acme', 'procedural', 'Legacy rule from before MEM-01', "
        "'active', 'proven', 'human_explicit', 0.85, 0.85, '2025-01-01T00:00:00Z', "
        "'2025-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    item = engine.get_item("pre-mem01")
    assert item is not None
    assert item["text"] == "Legacy rule from before MEM-01"   # nothing lost
    assert item["type"] == "procedure"                        # backfilled from level
    assert item["scope"] == "project:acme"                    # backfilled from project
    assert item["valid_from"] == "2025-01-01T00:00:00Z"        # backfilled from created_at
    assert item["valid_until"] == ""
    assert item["provenance"] == {}
