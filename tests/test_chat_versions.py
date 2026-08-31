"""Chat versions: the tail an edit deletes is kept, and can be put back.

Editing a message truncates the chat. Before this, the answer that was already
there was gone for good — on a local model that can be twenty minutes of work.
"""
import time

import pytest

from src import chat_versions as cv


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "_dir", lambda: str(tmp_path / "chat_versions"))
    yield


def _msgs(n=2, text="answer"):
    out = [{"role": "user", "content": "question"}]
    for i in range(n - 1):
        out.append({"role": "assistant", "content": f"{text} {i}"})
    return out


def test_a_saved_tail_can_be_listed_and_read_back():
    got = cv.save("s1", _msgs(3), keep_count=4, reason="edit")
    assert got and got["count"] == 3 and got["keep_count"] == 4
    assert "messages" not in got                     # summaries stay small
    rows = cv.list_versions("s1")
    assert len(rows) == 1 and rows[0]["reason"] == "edit"
    full = cv.get("s1", rows[0]["id"])
    assert [m["content"] for m in full["messages"]] == ["question", "answer 0", "answer 1"]


def test_the_preview_is_the_answer_not_the_question():
    cv.save("s1", [{"role": "user", "content": "why is the sky blue"},
                   {"role": "assistant", "content": "Rayleigh scattering, mostly."}], keep_count=0)
    assert cv.list_versions("s1")[0]["preview"] == "Rayleigh scattering, mostly."


def test_versions_are_newest_first():
    cv.save("s1", _msgs(2, "first"), keep_count=0)
    cv.save("s1", _msgs(2, "second"), keep_count=0)
    assert cv.list_versions("s1")[0]["preview"].startswith("second")


def test_sessions_do_not_see_each_others_versions():
    cv.save("a", _msgs(2), keep_count=0)
    assert cv.list_versions("b") == []


def test_saving_nothing_saves_nothing():
    assert cv.save("s1", [], keep_count=0) is None
    assert cv.list_versions("s1") == []


def test_only_the_last_n_versions_are_kept(monkeypatch):
    monkeypatch.setattr(cv, "_setting", lambda k, d: 3 if k == "chat_versions_keep" else d)
    for i in range(6):
        cv.save("s1", _msgs(2, f"v{i}"), keep_count=0)
    rows = cv.list_versions("s1")
    assert len(rows) == 3
    assert rows[0]["preview"].startswith("v5") and rows[-1]["preview"].startswith("v3")


def test_versions_older_than_the_window_are_dropped(monkeypatch):
    cv.save("s1", _msgs(2, "old"), keep_count=0)
    # Age the stored record past the window, then trigger a prune with a save.
    data = cv._load("s1")
    data["versions"][0]["created_at"] = time.time() - 10 * 24 * 3600
    cv._store("s1", data)
    cv.save("s1", _msgs(2, "new"), keep_count=0)
    rows = cv.list_versions("s1")
    assert len(rows) == 1 and rows[0]["preview"].startswith("new")


def test_dropping_and_clearing():
    cv.save("s1", _msgs(2), keep_count=0)
    vid = cv.list_versions("s1")[0]["id"]
    assert cv.drop("s1", vid) is True
    assert cv.drop("s1", vid) is False
    cv.save("s1", _msgs(2), keep_count=0)
    assert cv.clear("s1") == 1 and cv.list_versions("s1") == []


def test_a_hostile_session_id_never_escapes_the_folder():
    assert cv._path("../../etc/passwd") is None
    assert cv.save("../../etc/passwd", _msgs(2), keep_count=0) is None


def test_dataclass_messages_are_accepted():
    from core.models import ChatMessage
    cv.save("s1", [ChatMessage(role="assistant", content="hi", metadata={"a": 1})], keep_count=0)
    full = cv.get("s1", cv.list_versions("s1")[0]["id"])
    assert full["messages"][0] == {"role": "assistant", "content": "hi", "metadata": {"a": 1}}


def test_disabled_saves_nothing(monkeypatch):
    monkeypatch.setattr(cv, "_setting", lambda k, d: False if k == "chat_versions" else d)
    assert cv.save("s1", _msgs(2), keep_count=0) is None


def test_unreadable_store_degrades_to_empty(tmp_path):
    p = cv._path("s1")
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write("{not json")
    assert cv.list_versions("s1") == []
    assert cv.save("s1", _msgs(2), keep_count=0) is not None   # and recovers
