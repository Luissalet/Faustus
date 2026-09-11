"""src/session_draft.py — UX-01's server-side half of the composer draft
(see the module docstring for why this exists alongside Studio's own
localStorage draft). Same owner-scoping/atomic-write idiom
tests/test_chat_team.py already exercises for src/chat_team.py.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    from src import session_draft
    monkeypatch.setattr(session_draft, "DATA_DIR", tmp_path)
    return session_draft


def test_load_with_no_saved_draft_is_the_empty_record(store):
    assert store.load("session", "alice") == {"text": "", "attachment_ids": [], "updated_at": 0}


def test_save_then_load_round_trips(store):
    saved = store.save("session", "alice", "Hello, world", ["att1", "att2"])
    assert saved["text"] == "Hello, world"
    assert saved["attachment_ids"] == ["att1", "att2"]
    assert saved["updated_at"] > 0

    loaded = store.load("session", "alice")
    assert loaded == saved


def test_owner_scoping_keeps_drafts_separate(store):
    store.save("session", "alice", "Alice's draft", [])
    assert store.load("session", "bob")["text"] == ""
    assert store.load("session", "alice")["text"] == "Alice's draft"


def test_saving_empty_text_and_no_attachments_clears_the_slot(store):
    store.save("session", "alice", "something", [])
    assert store.load("session", "alice")["text"] == "something"
    cleared = store.save("session", "alice", "", [])
    assert cleared == {"text": "", "attachment_ids": [], "updated_at": 0}
    assert store.load("session", "alice") == {"text": "", "attachment_ids": [], "updated_at": 0}
    assert not store._path("session", "alice").exists()


def test_whitespace_only_text_with_no_attachments_also_clears(store):
    store.save("session", "alice", "hi", [])
    store.save("session", "alice", "   \n\t", [])
    assert store.load("session", "alice")["text"] == ""


def test_text_alone_with_no_attachments_is_kept_even_if_attachments_are_empty(store):
    saved = store.save("session", "alice", "keep me", [])
    assert saved["text"] == "keep me"


def test_attachments_alone_with_no_text_are_kept(store):
    saved = store.save("session", "alice", "", ["att1"])
    assert saved["attachment_ids"] == ["att1"]
    assert saved["text"] == ""


def test_text_over_the_cap_is_refused(store):
    with pytest.raises(ValueError, match="exceeds"):
        store.save("session", "alice", "x" * (store.MAX_TEXT_CHARS + 1), [])


def test_too_many_attachment_ids_is_refused(store):
    with pytest.raises(ValueError):
        store.save("session", "alice", "hi", [f"a{i}" for i in range(store.MAX_ATTACHMENTS + 1)])


def test_non_string_attachment_id_is_refused(store):
    with pytest.raises(ValueError):
        store.save("session", "alice", "hi", [123])


def test_corrupt_saved_file_reads_as_empty_not_a_crash(store):
    path = store._path("session", "alice")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.load("session", "alice") == {"text": "", "attachment_ids": [], "updated_at": 0}


def test_a_missing_session_id_is_refused(store):
    with pytest.raises(ValueError):
        store.save("", "alice", "hi", [])
