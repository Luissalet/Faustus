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


@pytest.mark.parametrize('text', ['á漢🙂\n' * 20_000 + 'END', 'x' * 3_192_506 + 'END'],
                         ids=['unicode_over_64K', 'three_million_chars'])
def test_long_text_round_trips_without_an_independent_character_cap(store, text):
    import hashlib
    saved = store.save('session', 'alice', text, ['att1'])
    loaded = store.load('session', 'alice')
    assert len(loaded['text']) == len(text)
    assert hashlib.sha256(loaded['text'].encode()).digest() == hashlib.sha256(text.encode()).digest()
    assert loaded['attachment_ids'] == ['att1'] and loaded['updated_at'] == saved['updated_at']


def test_long_text_survives_a_new_python_process(store):
    import hashlib
    import json
    import os
    import subprocess
    import sys
    text = 'á漢🙂\n' * 20_000 + 'END'
    store.save('session', 'alice', text, ['att1'])
    env = {**os.environ, 'ODYSSEUS_DATA_DIR': str(store.DATA_DIR)}
    code = ("import hashlib,json;from src.session_draft import load;d=load('session','alice');"
            "print(json.dumps({'chars':len(d['text']),'sha256':hashlib.sha256(d['text'].encode()).hexdigest(),"
            "'ids':d['attachment_ids']}))")
    result = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'chars': len(text), 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'ids': ['att1']}


def test_failed_atomic_write_preserves_the_previous_draft(store, monkeypatch):
    store.save('session', 'alice', 'previous', ['att1'])
    before = store._path('session', 'alice').read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Own fixture write failed')
    monkeypatch.setattr(store, 'atomic_write_text', fail)
    with pytest.raises(OSError):
        store.save('session', 'alice', 'á漢🙂' * 30_000, ['att2'])
    assert store._path('session', 'alice').read_bytes() == before
    assert store.load('session', 'alice')['text'] == 'previous'


@pytest.mark.parametrize('text', [None, False, 123, [], {}])
def test_text_still_must_be_a_string(store, text):
    with pytest.raises(ValueError, match='text must be a string'):
        store.save('session', 'alice', text, [])


def test_attachment_id_length_and_count_boundaries_are_unchanged(store):
    ids = ['a' * 300] * 64
    assert store.save('session', 'alice', 'draft', ids)['attachment_ids'] == ids
    for bad in (['a' * 301], ['   '], ['a'] * 65):
        with pytest.raises(ValueError):
            store.save('session', 'alice', 'draft', bad)


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
