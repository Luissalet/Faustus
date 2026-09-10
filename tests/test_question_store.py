"""question_store — durable record of an `ask_user` question and its answer.

CALL-07 / TASK-04: a stale, cancelled, or duplicate answer must be rejected
with a reason, and silence must never be read as consent (QA-13). Each test
here pins one of those rules; reverting question_store.py to a stub that
just returns {"ok": True} for resolve() makes every rejection test fail.
"""
from src import question_store


def _store(tmp_path):
    return question_store.Store(tmp_path / "questions.sqlite3")


def test_open_returns_open_status_and_never_auto_answers(tmp_path):
    store = _store(tmp_path)
    q = store.open("Which library?", session_id="s1", owner="alice",
                    options=[{"label": "A", "id": "opt_a"}, {"label": "B", "id": "opt_b"}])
    assert q["status"] == "open"
    assert q["answer"] is None
    # QA-13: fetching it again, with no resolve() ever called, must still
    # read "open" — never "answered". There is no timeout-implies-yes path.
    again = store.get(q["question_id"])
    assert again["status"] == "open"
    assert again["answer"] is None


def test_resolve_records_the_answer(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}])
    outcome = store.resolve(q["question_id"], {"option_id": "opt_a"})
    assert outcome["ok"] is True
    assert outcome["question"]["status"] == "answered"
    assert outcome["question"]["answer"] == {"option_id": "opt_a"}


def test_second_answer_is_rejected_dedupe(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}])
    first = store.resolve(q["question_id"], "A")
    assert first["ok"] is True
    second = store.resolve(q["question_id"], "B")
    assert second["ok"] is False
    assert second["reason"] == "already_answered"
    # The first answer must still stand, untouched by the rejected second one.
    assert store.get(q["question_id"])["answer"] == "A"


def test_cancelled_question_rejects_late_answer(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}])
    cancelled = store.cancel(q["question_id"], reason="model moved on")
    assert cancelled["ok"] is True
    late = store.resolve(q["question_id"], "A")
    assert late["ok"] is False
    assert late["reason"] == "cancelled"
    assert store.get(q["question_id"])["status"] == "cancelled"
    assert store.get(q["question_id"])["answer"] is None


def test_stale_revision_is_rejected(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}],
                    revision=3)
    outcome = store.resolve(q["question_id"], "A", revision=2)
    assert outcome["ok"] is False
    assert outcome["reason"] == "stale_revision"
    assert store.get(q["question_id"])["status"] == "open"
    # The correct revision still resolves it.
    assert store.resolve(q["question_id"], "A", revision=3)["ok"] is True


def test_expiry_never_becomes_an_answer(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}],
                    ttl_seconds=1)
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(seconds=5)).replace(microsecond=0)
    stamp = future.isoformat().replace("+00:00", "Z")
    # Directly force the sweep with a timestamp past the TTL to avoid a real
    # sleep in the test; expire_stale is the same code path a scheduler uses.
    swept = store.expire_stale(now=stamp)
    assert swept == 1
    record = store.get(q["question_id"])
    assert record["status"] == "expired"
    assert record["answer"] is None
    late = store.resolve(q["question_id"], "A")
    assert late["ok"] is False
    assert late["reason"] == "expired"


def test_new_open_question_supersedes_the_previous_open_one(tmp_path):
    """A model that asks a second question before the first was answered
    means the first is no longer the one to answer — an answer that crosses
    in flight for it must not land as consent for the new one."""
    store = _store(tmp_path)
    first = store.open("First?", session_id="s1", options=[{"label": "A"}, {"label": "B"}])
    second = store.open("Second?", session_id="s1", options=[{"label": "C"}, {"label": "D"}])
    assert store.get(first["question_id"])["status"] == "cancelled"
    assert store.get(second["question_id"])["status"] == "open"
    stale_answer = store.resolve(first["question_id"], "A")
    assert stale_answer["ok"] is False
    assert stale_answer["reason"] == "cancelled"


def test_resolve_unknown_question_id(tmp_path):
    store = _store(tmp_path)
    outcome = store.resolve("qst_does_not_exist", "A")
    assert outcome["ok"] is False
    assert outcome["reason"] == "not_found"


def test_cancel_an_already_answered_question_is_refused(tmp_path):
    store = _store(tmp_path)
    q = store.open("Pick one", session_id="s1", options=[{"label": "A"}, {"label": "B"}])
    store.resolve(q["question_id"], "A")
    outcome = store.cancel(q["question_id"])
    assert outcome["ok"] is False
    assert outcome["reason"] == "already_answered"
