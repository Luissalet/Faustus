"""Explicit changes of value in learned memories build the fact's chain."""
import pytest

from src.brain import db as brain_db, entities, extract, fact_history

OWNER = "chg-owner"
AT = "2026-10-01T10:00:00+00:00"


@pytest.fixture(autouse=True)
def store(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    entities.upsert_entity(OWNER, "Ada", type="person")
    yield
    brain_db.use_dir(None)


def _rows(rel):
    out = []
    for r in entities.list_relations(OWNER, include_closed=True):
        if r["rel"] == rel:
            out.append((fact_history._label(r), r["status"], r["superseded_by"], r["id"]))
    return sorted(out)


def _by_value(rel):
    return {value: (status, sup, rid) for value, status, sup, rid in _rows(rel)}


@pytest.mark.parametrize("text,old,new", [
    ("Ada ya no usa Vim, ahora usa Helix.", "Vim", "Helix"),
    ("Ada no longer uses Emacs, now she uses Zed.", "Emacs", "Zed"),
    ("Ada dejó de usar Notion y ahora usa Obsidian.", "Notion", "Obsidian"),
    ("Ada stopped using Trello and now uses Linear.", "Trello", "Linear"),
])
def test_change_closes_old_and_opens_new(text, old, new):
    extract.extract_source(OWNER, "mem:1", text, created_at=AT)
    rows = _by_value("uses")
    assert set(rows) == {old, new}
    assert rows[new][0] == "active"
    assert rows[old][0] == "superseded" and rows[old][1] == rows[new][2]
    view = fact_history.fact_history(OWNER, "Ada", "uses")
    assert [e["value"] for e in view["current"]] == [new]
    assert view["history_line"] == f"antes: {old}, hasta 2026-10"
    old_event = next(e for e in view["events"] if e["value"] == old)
    new_event = next(e for e in view["events"] if e["value"] == new)
    assert new_event["supersedes"] == old_event["id"]
    assert old_event["valid_until"].startswith("2026-10-01")
    assert new_event["valid_from"].startswith("2026-10-01")


def test_change_closes_the_value_already_stored():
    extract.extract_source(OWNER, "mem:old", "Ada usa Vim.", created_at="2026-05-01T09:00:00+00:00")
    assert _by_value("uses")["Vim"][0] == "active"
    extract.extract_source(OWNER, "mem:new", "Ada ya no usa Vim, ahora usa Helix.", created_at=AT)
    rows = _rows("uses")
    assert len(rows) == 2  # the stored Vim row is closed, not duplicated
    by = _by_value("uses")
    assert by["Vim"][0] == "superseded" and by["Vim"][1] == by["Helix"][2]
    vim = next(r for r in entities.list_relations(OWNER, include_closed=True)
               if fact_history._label(r) == "Vim")
    # an undated relation starts when it was asserted; its window never ends before it starts
    assert vim["valid_until"] and vim["valid_until"] >= vim["valid_from"]
    assert set(vim["evidence"]) == {"mem:old", "mem:new"}


def test_functional_change_keeps_one_row_per_value():
    entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    extract.extract_source(OWNER, "mem:a", "Ada trabaja en Cordera Labs.",
                           created_at="2025-01-10T09:00:00+00:00")
    extract.extract_source(OWNER, "mem:b", "Ada no longer works at Cordera Labs, now at Bluehaven Studio.",
                           created_at=AT)
    by = _by_value("works_at")
    assert set(by) == {"Cordera Labs", "Bluehaven Studio"}
    assert by["Cordera Labs"][0] == "superseded" and by["Cordera Labs"][1] == by["Bluehaven Studio"][2]
    assert by["Bluehaven Studio"][0] == "active"


def test_stop_without_new_value_only_closes():
    extract.extract_source(OWNER, "mem:1", "Ada ya no prefiere Python.", created_at=AT)
    by = _by_value("prefers")
    assert by == {"Python": ("superseded", "", by["Python"][2])}
    view = fact_history.fact_history(OWNER, "Ada", "prefers")
    assert view["current"] == [] and view["history_line"] == "antes: Python, hasta 2026-10"


def test_tail_without_new_value_is_not_read_as_one():
    extract.extract_source(OWNER, "mem:1", "Ada ya no usa Vim, lo cual sorprende.", created_at=AT)
    assert [v for v, *_ in _rows("uses")] == ["Vim"]


def test_plain_sentences_unchanged():
    extract.extract_source(OWNER, "mem:1", "Ada usa Vim.", created_at=AT)
    assert _by_value("uses")["Vim"][0] == "active"
    assert extract._extract_change("Ada usa Vim y Helix.") is None


def test_reextraction_is_idempotent():
    text = "Ada ya no usa Vim, ahora usa Helix."
    extract.extract_source(OWNER, "mem:1", text, created_at=AT)
    extract.extract_source(OWNER, "mem:1", text, created_at=AT)
    assert len(_rows("uses")) == 2


def test_parse_shapes():
    c = extract._extract_change("Ada ya no vive en Madrid; ahora vive en Lisboa")
    assert c == {"subject": "Ada", "rel": "lives_in", "old": "Madrid", "new_rel": "lives_in",
                 "new": "Lisboa"}
    c = extract._extract_change("Ada ya no trabaja en Initech, ahora trabaja sobre Faustus")
    assert c["rel"] == "works_at" and c["new_rel"] == "works_on" and c["new"] == "Faustus"
    c = extract._extract_change("Yo ya no uso Windows")
    assert c["subject"] == "Yo" and c["old"] == "Windows" and c["new"] == ""


def test_older_source_read_later_does_not_reopen():
    # the sweep may read the change before the older memory that stated the value
    extract.extract_source(OWNER, "mem:new", "Ada ya no usa Vim, ahora usa Helix.", created_at=AT)
    extract.extract_source(OWNER, "mem:old", "Ada usa Vim para todo.",
                           created_at="2026-05-01T09:00:00+00:00")
    by = _by_value("uses")
    assert set(by) == {"Vim", "Helix"} and by["Vim"][0] == "superseded"
    vim = next(r for r in entities.list_relations(OWNER, include_closed=True)
               if fact_history._label(r) == "Vim")
    assert set(vim["evidence"]) == {"mem:old", "mem:new"}
    # a source written AFTER the change does reopen it
    extract.extract_source(OWNER, "mem:later", "Ada usa Vim otra vez.",
                           created_at="2026-10-05T09:00:00+00:00")
    assert sorted(s for v, s, *_ in _rows("uses") if v == "Vim") == ["active", "superseded"]
