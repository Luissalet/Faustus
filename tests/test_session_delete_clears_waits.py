"""Deleting a chat takes its pending cards and open questions with it."""
from routes import session_routes
from src import question_store
from src.tool_approvals import tool_approval_store


def test_deleted_session_cards_and_questions_go(monkeypatch):
    forgotten, cancelled = [], []
    monkeypatch.setattr(tool_approval_store, "forget_session", lambda sid: forgotten.append(sid) or 1)
    monkeypatch.setattr(question_store, "list_open", lambda owner=None, limit=50: [
        {"question_id": "q1", "session_id": "gone"}, {"question_id": "q2", "session_id": "other"}])
    monkeypatch.setattr(question_store, "cancel_question", lambda qid, reason="", owner=None: cancelled.append((qid, reason)) or {"ok": True})
    session_routes._stop_runs_for_deleted_sessions(["gone"])
    assert forgotten == ["gone"]
    assert cancelled == [("q1", "session_deleted")]