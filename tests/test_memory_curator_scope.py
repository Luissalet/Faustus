"""Curator mutation boundaries, exercised against a disposable SQLite store."""
from datetime import datetime, timedelta, timezone

import pytest

from src import memory_curator as curator
from src import memory_engine as engine

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
TEXT = "Run the Atlas verification suite before publishing a release"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    yield
    engine.reset_vector_store()


def add(**kwargs):
    return engine.add_item(TEXT, now=NOW, **kwargs)


@pytest.mark.parametrize("fuzzy", [False, True])
def test_distinct_sessions_survive(store, fuzzy):
    a = add(owner="alice", project="atlas", session_id="private-a")
    b = engine.add_item(TEXT + (" today" if fuzzy else ""), now=NOW,
                        owner="alice", project="atlas", session_id="private-b")
    assert curator.curate("alice", "atlas", now=NOW)["deduped"] == 0
    assert engine.get_item(a["id"]) and engine.get_item(b["id"])


@pytest.mark.parametrize("boundary", ["owner", "project", "session", "scope", "window", "level"])
def test_antipattern_cannot_demote_across_boundary(store, boundary):
    a = add(owner="alice", project="atlas", session_id="private-a")
    options = dict(owner="alice", project="atlas", session_id="private-a")
    if boundary == "owner":
        options["owner"] = "bob"
    elif boundary == "project":
        options["project"] = "other"
    elif boundary == "session":
        options["session_id"] = "private-b"
    elif boundary == "scope":
        options = dict(owner="", project="")
    elif boundary == "level":
        options["level"] = "procedural"
    else:
        options["valid_until"] = NOW + timedelta(days=2)
    b = add(**options)
    b.update(status="anti_pattern", text="AVOID: " + TEXT, inverted_from=TEXT)
    engine.save_item(b)
    assert curator.curate(now=NOW)["conflicts"] == 0
    assert engine.get_item(a["id"])["status"] == "active"


def test_same_scope_duplicates_merge_evidence_and_conflicts_apply(store):
    for ref in ("one", "two"):
        add(owner="alice", project="atlas", session_id="same",
            evidence=[{"kind": "document", "ref": ref}])
    assert curator.curate(now=NOW)["deduped"] == 1
    rows = engine.scoped_items("alice", "atlas")
    assert {e["ref"] for e in rows[0]["evidence"]} == {"one", "two"}
    anti = add(owner="alice", project="atlas", session_id="same")
    anti.update(status="anti_pattern", text="AVOID: " + TEXT, inverted_from=TEXT)
    engine.save_item(anti)
    assert curator.curate(now=NOW)["conflicts"] == 1
    assert curator.curate(now=NOW)["conflicts"] == 0


def test_project_session_origin_is_not_a_scope_boundary(store):
    for origin in ("source-a", "source-b"):
        row = add(owner="alice", project="atlas")
        row["session_id"] = origin
        engine.save_item(row)
    assert curator.curate(now=NOW)["deduped"] == 1


def test_private_antipattern_does_not_demote_visible_global_rule(store):
    global_rule = add()
    private = add(owner="alice", project="atlas")
    private.update(status="anti_pattern", text="AVOID: " + TEXT, inverted_from=TEXT)
    engine.save_item(private)
    assert curator.curate("alice", "atlas", now=NOW)["conflicts"] == 0
    assert engine.get_item(global_rule["id"])["status"] == "active"


def test_project_and_session_scope_remain_distinct(store):
    add(owner="alice", project="atlas")
    add(owner="alice", project="atlas", session_id="private")
    assert curator.curate("alice", "atlas", now=NOW)["deduped"] == 0


@pytest.mark.parametrize("field,value", [("owner", "bob"), ("project", "other")])
def test_distinct_owner_or_project_never_deduplicate(store, field, value):
    add(owner="alice", project="atlas")
    options = dict(owner="alice", project="atlas")
    options[field] = value
    add(**options)
    assert curator.curate(now=NOW)["deduped"] == 0
