"""MEM-02 — corrección, olvido y borrado propagado (src/memory_engine.py).

`forget()`/`correct()` are the human end of the loop QA-34 describes: a row
must not just disappear from `items`, it must leave a `tombstones` row that
survives a later reindex/import/consolidation, so the same text cannot come
back under a new id unless a human explicitly says it again
(`trust_class="human_explicit"`).

Before this change `delete_item` was a physical DELETE + `_unindex` with no
tombstone at all (see docs/spec/v2/MAPA_REUTILIZACION.md, MEM-02 row) — an
automated re-insertion of the same text (an extractor, a consolidation pass)
would have gone straight back into `items`. The tests below fail on that
prior state: reverting `src/memory_engine.py` to drop `forget`/`correct`/
`is_tombstoned`/`list_tombstones` (or the `respect_tombstones` guard in
`add_item`) makes every test here either error (AttributeError) or fail the
assertion that resurrection is blocked.
"""

import os
import sys
from datetime import datetime, timezone

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


def _fact(text="The cart total excludes shipping", **kw):
    kw.setdefault("owner", "luis")
    kw.setdefault("project", "acme")
    kw.setdefault("level", "semantic")
    kw.setdefault("trust_class", "human_explicit")
    kw.setdefault("now", NOW)
    return engine.add_item(text, **kw)


def test_forget_deletes_the_row_and_leaves_a_tombstone(store):
    item = _fact()
    tombstone = engine.forget(item["id"], reason="user said it was wrong", now=NOW)

    assert tombstone is not None
    assert tombstone["item_id"] == item["id"]
    assert engine.get_item(item["id"]) is None                # row is gone
    rows = engine.list_tombstones(owner="luis")
    assert any(row["item_id"] == item["id"] for row in rows)   # tombstone remains


def test_forget_of_a_missing_item_is_a_noop_not_an_error(store):
    assert engine.forget("does-not-exist") is None
    assert engine.list_tombstones() == []


def test_neither_search_nor_pack_ever_returns_a_forgotten_memory(store):
    item = _fact("Ship on Fridays only", level="semantic")
    engine.forget(item["id"], now=NOW)

    hits = engine.search("Ship on Fridays", "luis", "acme", now=NOW)
    assert all(h["id"] != item["id"] for h in hits)

    detail = engine.pack_detail("luis", "acme", "Ship on Fridays", 2000, now=NOW)
    assert item["id"] not in detail["ids"]


def test_forget_blocks_automatic_resurrection_of_the_same_text(store):
    """The QA-34 scenario: an automated path (an extractor re-noticing the
    same fact, a consolidation pass rebuilding from a stale snapshot) must
    not bring back what a human explicitly forgot."""
    item = _fact("Always deploy on a green build", level="procedural")
    engine.forget(item["id"], now=NOW)

    with pytest.raises(engine.MemoryEngineError):
        engine.add_item(
            "Always deploy on a green build",
            owner="luis", project="acme", level="procedural",
            trust_class="agent_assertion", now=NOW,
        )
    # A DIFFERENT scope (different project) is unaffected — the tombstone is
    # per-scope, not a global ban on the sentence.
    other = engine.add_item(
        "Always deploy on a green build",
        owner="luis", project="other-project", level="procedural",
        trust_class="agent_assertion", now=NOW,
    )
    assert other["text"] == "Always deploy on a green build"


def test_forget_never_blocks_an_explicit_human_restatement(store):
    """The "excepciones de archivos explícitos acordes a política" half of
    QA-34: a human is always allowed to say the same thing again — that is a
    new decision, not a resurrection."""
    item = _fact("Prefer tabs over spaces")
    engine.forget(item["id"], now=NOW)

    revived = engine.add_item(
        "Prefer tabs over spaces",
        owner="luis", project="acme", trust_class="human_explicit", now=NOW,
    )
    assert revived["text"] == "Prefer tabs over spaces"
    assert engine.get_item(revived["id"]) is not None


def test_forget_removes_the_vector_embedding_too(store):
    class RecordingVectors:
        healthy = True

        def __init__(self):
            self.added, self.removed = [], []

        def add(self, memory_id, text):
            self.added.append(memory_id)

        def remove(self, memory_id):
            self.removed.append(memory_id)

        def search(self, query, k=8):
            return []

    vectors = RecordingVectors()
    engine.set_vector_store(vectors)
    item = _fact("Retry uploads up to three times")
    assert item["id"] in vectors.added

    engine.forget(item["id"], now=NOW)
    assert item["id"] in vectors.removed


def test_correct_replaces_text_and_links_provenance_to_the_original(store):
    item = _fact("The API key rotates every 30 days")
    corrected = engine.correct(item["id"], "The API key rotates every 90 days", now=NOW)

    assert corrected is not None
    assert corrected["text"] == "The API key rotates every 90 days"
    assert corrected["provenance"]["corrected_from"] == item["id"]
    assert engine.get_item(item["id"]) is None                 # original is gone
    assert any(row["item_id"] == item["id"]
               for row in engine.list_tombstones(owner="luis"))  # …and tombstoned

    # The corrected text must land even though it would otherwise collide
    # with nothing (no tombstone on the NEW text) — sanity check it is a real,
    # searchable item and not just a return value.
    hits = engine.search("API key rotates", "luis", "acme", now=NOW)
    assert any(h["id"] == corrected["id"] for h in hits)


def test_correct_of_a_missing_item_returns_none(store):
    assert engine.correct("does-not-exist", "new text") is None


def test_correct_with_empty_text_keeps_the_original(store):
    """Found by the route that wraps it: an empty replacement must not
    tombstone the original first and raise afterwards."""
    item = _fact("coffee black in the morning")
    with pytest.raises(engine.MemoryEngineError):
        engine.correct(item["id"], "   ")
    assert engine.get_item(item["id"]) is not None
    assert not engine.is_tombstoned("coffee black in the morning", owner=item.get("owner", ""), project=item.get("project", ""))
