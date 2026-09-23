"""End-to-end test for MEM-TEMPORAL: memory_engine.add_item's automatic
temporal fill, memory_conflicts' automatic supersede of a functional-style
predicate, and memory_engine.search/list_items(as_of=...).

This lives under the `test_brain_temporal*` pattern (Lot B's tests) even
though it exercises `src/memory_engine.py` and `src/memory_conflicts.py`
directly, because those two modules only received SURGICAL edits for this
feature — the feature itself, and the thing worth end-to-end testing, is
the temporal behaviour, not a new module of its own.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_conflicts as conflicts  # noqa: E402
from src import memory_engine as engine  # noqa: E402

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
BETWEEN = T0 + (T1 - T0) / 2
AFTER = T1 + timedelta(days=1)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


def test_supersede_end_to_end(store):
    old = engine.add_item("Ada works at Cordera Labs", owner="alice",
                          trust_class="human_explicit", now=T0)
    new = engine.add_item("Ada works at Bluehaven", owner="alice",
                          trust_class="human_explicit", now=T1)

    # The conflict was recorded and immediately resolved as `superseded`,
    # never left `open`.
    open_rows = conflicts.list_conflicts(owner="alice", status="open")
    assert open_rows == []
    superseded_rows = conflicts.list_conflicts(owner="alice", status="superseded")
    assert len(superseded_rows) == 1
    conflict = superseded_rows[0]
    assert conflict["old_id"] == old["id"]
    assert conflict["new_id"] == new["id"]

    # The old item's window was closed at the new item's valid_from.
    old_reloaded = engine.get_item(old["id"])
    assert old_reloaded["valid_until"] == new["valid_from"]

    # No ranking penalty / contradiction marker for a superseded pair.
    public_old = engine.public_item(old_reloaded, now=T1)
    assert public_old["open_conflict"] is None
    assert "(contradicted by a newer memory)" not in public_old["text"]

    # search(as_of=<between>) sees only the old fact; search() "now" (after
    # both) sees only the new one.
    as_of_hits = engine.search("Ada", owner="alice", now=AFTER, as_of=BETWEEN,
                               statuses=("active",), touch_hits=False)
    assert [h["id"] for h in as_of_hits] == [old["id"]]

    now_hits = engine.search("Ada", owner="alice", now=AFTER,
                             statuses=("active",), touch_hits=False)
    assert [h["id"] for h in now_hits] == [new["id"]]

    # list_items(as_of=...) applies the same window filter; with no as_of it
    # keeps returning every row (today's default behaviour, unchanged).
    assert {i["id"] for i in engine.list_items(owner="alice")} == {old["id"], new["id"]}
    at_between = engine.list_items(owner="alice", as_of=BETWEEN)
    assert [i["id"] for i in at_between] == [old["id"]]

    # unsupersede reopens the conflict and restores the old item's window.
    reopened = conflicts.unsupersede(conflict["id"])
    assert reopened["status"] == "open"
    restored = engine.get_item(old["id"])
    assert restored["valid_until"] == ""
    public_restored = engine.public_item(restored, now=T1)
    assert public_restored["open_conflict"] is not None
    assert "(contradicted by a newer memory)" in public_restored["text"]


def test_negation_conflicts_are_never_superseded(store):
    old = engine.add_item("Alice prefers tabs", owner="alice", trust_class="human_explicit", now=T0)
    engine.add_item("Alice does not prefer tabs", owner="alice", trust_class="human_explicit", now=T1)

    open_rows = conflicts.list_conflicts(owner="alice", status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["reason"] == "negation"
    assert open_rows[0]["old_id"] == old["id"]

    old_reloaded = engine.get_item(old["id"])
    assert old_reloaded["valid_until"] == ""  # untouched


def test_supersede_disabled_by_setting_keeps_conflict_open(store, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "memory_temporal_supersede" else default,
    )
    old = engine.add_item("Bruno works at Cordera Labs", owner="alice",
                          trust_class="human_explicit", now=T0)
    engine.add_item("Bruno works at Bluehaven", owner="alice",
                    trust_class="human_explicit", now=T1)
    open_rows = conflicts.list_conflicts(owner="alice", status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["old_id"] == old["id"]


def test_resolve_keep_superseded_applies_manually_outside_the_auto_set(store):
    # "uses" is NOT in the automatic-supersede predicate set (existing
    # conflict tests rely on it staying `open`), so this pair is left open —
    # `resolve(id, keep="superseded")` still applies the resolution by hand.
    old = engine.add_item("the project uses Python", owner="alice",
                          trust_class="human_explicit", now=T0)
    new = engine.add_item("the project uses Rust", owner="alice",
                          trust_class="human_explicit", now=T1)
    open_rows = conflicts.list_conflicts(owner="alice", status="open")
    assert len(open_rows) == 1

    resolved = conflicts.resolve(open_rows[0]["id"], "superseded")
    assert resolved["status"] == "superseded"
    assert engine.get_item(old["id"])["valid_until"] == new["valid_from"]
    assert conflicts.list_conflicts(owner="alice", status="open") == []


def test_temporal_fill_on_add_item_and_past_state_provenance(store):
    dated = engine.add_item("Ada worked at Cordera Labs desde marzo de 2025",
                            owner="alice", trust_class="human_explicit", now=T1)
    assert dated["valid_from"] == "2025-03-01T00:00:00Z"

    past = engine.add_item("Ada ya no trabaja en Cordera Labs", owner="alice",
                           trust_class="human_explicit", now=T1)
    assert past["valid_until"] == ""
    assert past["provenance"].get("temporal_state") == "past"

    explicit = engine.add_item("Ada works at Cordera Labs", owner="alice",
                               trust_class="human_explicit", now=T1,
                               valid_from="2020-01-01T00:00:00Z")
    # An explicit valid_from short-circuits the parser entirely.
    assert explicit["valid_from"] == "2020-01-01T00:00:00Z"


def test_temporal_fill_can_be_disabled(store, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "memory_temporal_parse" else default,
    )
    item = engine.add_item("Ada works at Cordera Labs desde marzo de 2025",
                           owner="alice", trust_class="human_explicit", now=T1)
    assert item["valid_from"] == engine._iso(T1)
