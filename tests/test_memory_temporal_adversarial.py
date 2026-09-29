"""Graphiti-inspired probes: distinct events survive curation and retrieval."""

from datetime import datetime, timedelta, timezone

import pytest

from src import memory_curator as curator
from src import memory_engine as engine


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    yield
    engine.reset_vector_store()


@pytest.mark.parametrize("left,right", [
    ("Alice shipped 20 Atlas widgets to the Madrid office during March 2026.",
     "Alice shipped 20 Atlas widgets to the Madrid office during April 2026."),
    ("Alice shipped 20 Atlas widgets to the Madrid office during March 2026.",
     "Alice shipped 30 Atlas widgets to the Madrid office during March 2026."),
    ("Alice shipped only 20 Atlas widgets to the Madrid office during March 2026.",
     "Alice shipped 20 Atlas widgets to the Madrid office during March 2026."),
    ("Alice entregó 20 piezas Atlas al almacén de Madrid durante marzo de 2026.",
     "Alice entregó 20 piezas Atlas al almacén de Madrid durante abril de 2026."),
    ("Alice ships 20 Atlas widgets to the Madrid office every Monday.",
     "Alice ships 20 Atlas widgets to the Madrid office every Tuesday."),
])
def test_distinct_events_survive_curation_and_remain_retrievable(store, left, right):
    # Shared context must put the pair above the fuzzy threshold; otherwise
    # this would never exercise protection of changed factual details.
    suffix = " This shipment was recorded in the internal operations ledger with full provenance."
    left, right = left + suffix, right + suffix
    assert curator.get_text_similarity(left, right) > engine.DEDUPE_SIMILARITY
    ids = {engine.add_item(text, owner="alice", project="atlas", level="episodic",
                           trust_class="human_explicit", now=NOW)["id"]
           for text in (left, right)}
    report = curator.curate("alice", "atlas", now=NOW)
    assert report["deduped"] == 0
    for item_id in ids:
        item = engine.get_item(item_id)
        assert item and item["status"] == "active" and not item["valid_until"]
    found = {hit["id"] for hit in engine.search(
        "Alice Atlas Madrid", owner="alice", project="atlas", now=NOW, touch_hits=False)}
    assert ids <= found


@pytest.mark.parametrize("wording", [
    "Alice covers Atlas customer support for the regional Madrid office.",
    "Alice covers Atlas customer support for the regional Madrid office today.",
])
def test_repeated_state_with_disjoint_windows_preserves_both_intervals(store, wording):
    text = "Alice covers Atlas customer support for the regional Madrid office."
    assert curator.get_text_similarity(text, wording) > engine.DEDUPE_SIMILARITY
    old = engine.add_item(text, owner="alice", project="atlas",
                          trust_class="human_explicit", now=NOW,
                          valid_from=NOW - timedelta(days=20),
                          valid_until=NOW - timedelta(days=10))
    current = engine.add_item(wording, owner="alice", project="atlas",
                              trust_class="human_explicit", now=NOW,
                              valid_from=NOW - timedelta(days=5))
    assert curator.curate("alice", "atlas", now=NOW)["deduped"] == 0

    def recalled(at):
        return {hit["id"] for hit in engine.search(
            "Alice Atlas support", owner="alice", project="atlas", now=at,
            touch_hits=False)}

    assert recalled(NOW - timedelta(days=15)) == {old["id"]}
    assert recalled(NOW - timedelta(days=7)) == set()
    assert recalled(NOW) == {current["id"]}


@pytest.mark.parametrize("change", ["start", "end", "open_end"])
def test_overlapping_windows_are_not_merged(store, change):
    start, end = NOW - timedelta(days=20), NOW + timedelta(days=10)
    first = engine.add_item("Alice covers Atlas support.", owner="alice", now=NOW,
                            valid_from=start, valid_until=end)
    second = engine.add_item("Alice covers Atlas support.", owner="alice", now=NOW,
                             valid_from=start + timedelta(days=5) if change == "start" else start,
                             valid_until=None if change == "open_end" else
                             end + timedelta(days=5) if change == "end" else end)
    assert curator.curate("alice", now=NOW)["deduped"] == 0
    assert engine.get_item(first["id"]) and engine.get_item(second["id"])


def test_identical_windows_still_merge_evidence_and_remain_idempotent(store):
    for ref in ("shift-plan", "supervisor-confirmation"):
        engine.add_item("Alice covers Atlas support.", owner="alice", now=NOW,
                        valid_from=NOW - timedelta(days=5),
                        evidence=[{"kind": "document", "ref": ref}])
    assert curator.curate("alice", now=NOW)["deduped"] == 1
    assert curator.curate("alice", now=NOW)["deduped"] == 0
    hits = engine.search("Alice Atlas support", owner="alice", now=NOW, touch_hits=False)
    assert len(hits) == 1
    survivor = engine.get_item(hits[0]["id"])
    assert {e["ref"] for e in survivor["evidence"]} == {"shift-plan", "supervisor-confirmation"}


@pytest.mark.parametrize("suffix", ["", " today"])
def test_ordinary_repeated_insertions_at_different_times_still_deduplicate(store, suffix):
    text = "Alice covers Atlas customer support for the regional Madrid office"
    engine.add_item(text, owner="alice", now=NOW - timedelta(hours=1))
    engine.add_item(text + suffix, owner="alice", now=NOW)
    assert curator.curate("alice", now=NOW)["deduped"] == 1
    assert curator.curate("alice", now=NOW)["deduped"] == 0


def test_explicit_future_start_does_not_merge_with_default_start(store):
    text = "Alice covers Atlas support."
    current = engine.add_item(text, owner="alice", now=NOW)
    future = engine.add_item(text, owner="alice", now=NOW,
                             valid_from=NOW + timedelta(days=5))
    assert curator.curate("alice", now=NOW)["deduped"] == 0
    assert engine.get_item(current["id"]) and engine.get_item(future["id"])


def test_single_value_state_can_still_supersede_older_value(store, monkeypatch):
    from src import settings

    original = settings.get_setting
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None:
                        True if key in {"memory_conflict_detection", "memory_temporal_supersede"}
                        else original(key, default))
    old = engine.add_item("Alice lives in Madrid", owner="alice", now=NOW - timedelta(days=2))
    current = engine.add_item("Alice lives in Lisbon", owner="alice", now=NOW - timedelta(days=1))
    assert engine.get_item(old["id"])["valid_until"]
    found = {hit["id"] for hit in engine.search(
        "Alice lives", owner="alice", now=NOW, touch_hits=False)}
    assert current["id"] in found and old["id"] not in found
