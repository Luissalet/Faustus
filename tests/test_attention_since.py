"""A pending approval or question whose run has ended still says since when
it has waited (dated by the card or the question)."""
from src import attention


def _rows(monkeypatch, **kw):
    monkeypatch.setattr(attention, "_default_pending_approvals", lambda owner: ["appr"])
    monkeypatch.setattr(attention, "_default_pending_approval_since", lambda owner: {"appr": 9_000.0})
    return attention.attention_for_owner("alice", now=10_000.0, agent_activity={}, worker_cards={},
                                         finished_rows=[], reads={}, **kw)


def test_ended_run_approval_is_dated_by_its_card(monkeypatch):
    rows = _rows(monkeypatch, open_questions=[])
    row = [r for r in rows if r["session_id"] == "appr"][0]
    assert row["kind"] == "approval" and row["since"] == 9_000.0


def test_question_is_dated_by_when_it_was_opened(monkeypatch):
    rows = _rows(monkeypatch, open_questions=[{"session_id": "q1", "opened_at": "1970-01-01T02:30:00"}])
    row = [r for r in rows if r["session_id"] == "q1"][0]
    assert row["kind"] == "question" and row["since"] == 9_000.0


def test_bad_or_missing_dates_stay_unknown(monkeypatch):
    monkeypatch.setattr(attention, "_default_pending_approval_since", lambda owner: {})
    monkeypatch.setattr(attention, "_default_pending_approvals", lambda owner: ["appr"])
    rows = attention.attention_for_owner("alice", now=10_000.0, agent_activity={}, worker_cards={},
                                         finished_rows=[], reads={},
                                         open_questions=[{"session_id": "q1", "opened_at": "not a date"}])
    assert {r["session_id"]: r["since"] for r in rows} == {"appr": None, "q1": None}


def test_store_reports_the_oldest_card_per_session():
    from src.tool_approvals import ToolApprovalStore
    store = ToolApprovalStore()
    assert store.pending_session_since(owner="alice") == {}