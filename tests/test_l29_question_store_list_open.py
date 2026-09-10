"""L29 (integrates L28's ACT-03 note): `question_store.Store.list_open()` —
the query the activity tray's "answer this" queue runs. Reverting
list_open() (or the module-level `list_open` convenience) makes every test
below fail with AttributeError or a wrong result set.
"""
from src import question_store


def _store(tmp_path):
    return question_store.Store(tmp_path / "questions.sqlite3")


def test_lists_only_open_questions_most_recent_first(tmp_path):
    store = _store(tmp_path)
    q1 = store.open("First?", session_id="s1", owner="alice")
    q2 = store.open("Second?", session_id="s2", owner="alice", supersede_open=False)
    store.resolve(q1["question_id"], "A")
    open_ids = [q["question_id"] for q in store.list_open(owner="alice")]
    assert open_ids == [q2["question_id"]]


def test_owner_filter_is_a_positive_match_sec_06(tmp_path):
    store = _store(tmp_path)
    store.open("Alice's question", session_id="s1", owner="alice")
    assert store.list_open(owner="bob") == []
    assert len(store.list_open(owner="alice")) == 1
    # Case/whitespace-insensitive, same normalization as get()/resolve().
    assert len(store.list_open(owner=" Alice ")) == 1


def test_owner_none_lists_every_owner(tmp_path):
    store = _store(tmp_path)
    store.open("Q1", session_id="s1", owner="alice")
    store.open("Q2", session_id="s2", owner="bob", supersede_open=False)
    assert len(store.list_open(owner=None)) == 2


def test_a_cancelled_or_answered_question_never_appears(tmp_path):
    store = _store(tmp_path)
    q1 = store.open("Q1", session_id="s1", owner="alice")
    q2 = store.open("Q2", session_id="s2", owner="alice", supersede_open=False)
    store.resolve(q1["question_id"], "A")
    store.cancel(q2["question_id"])
    assert store.list_open(owner="alice") == []


def test_an_expired_question_is_settled_and_excluded(tmp_path):
    store = _store(tmp_path)
    q = store.open("Q1", session_id="s1", owner="alice", ttl_seconds=1)
    # Force it into the past without sleeping the test suite.
    import sqlite3
    db = sqlite3.connect(store.path)
    db.execute("UPDATE questions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (q["question_id"],))
    db.commit()
    db.close()
    assert store.list_open(owner="alice") == []
    assert store.get(q["question_id"])["status"] == "expired"


def test_module_level_convenience_matches_the_store_method(tmp_path, monkeypatch):
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "questions.sqlite3")
    question_store.open_question("Q1", session_id="s1", owner="alice")
    assert len(question_store.list_open(owner="alice")) == 1
