import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database, session_manager
from core.models import ChatMessage
from src import context_compactor as cc


@pytest.fixture
def manager(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "continuity.db").as_posix())
    factory = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(session_manager, "SessionLocal", factory)
    monkeypatch.setattr(cc, "post_compact_reminder", lambda *a: None)
    manager = session_manager.SessionManager()
    import core.models
    monkeypatch.setattr(core.models, "get_session_manager_instance", lambda: manager)
    yield manager
    engine.dispose()


def prepare(manager):
    manager.create_session(session_id="continuity", name="Test", endpoint_url="http://local", model="test")
    for role, text in [("user", "Do not send any email. Review draft."),
                       ("assistant", "Draft ready."), ("user", "Continue review.")]:
        manager.add_message("continuity", ChatMessage(role, text))
    session = manager.get_session("continuity")
    rows = [{"role": row.role, "content": row.content} for row in session.history]
    cc.annotate_history_positions(session, rows)
    return session, rows


@pytest.mark.parametrize("deferred", [False, True])
def test_preserved_restriction_survives_real_database_reopen(manager, deferred):
    session, rows = prepare(manager)
    state = {}
    live, _, _ = cc._finish_compaction(
        session, rows, [], rows[:2], rows[2:], "Draft reviewed.", 1000, 2000,
        None, not deferred, state, 2, kind="llm_summary",
    )
    if deferred:
        assert len(session.history) == 3
        cc.apply_compaction_state(session, state)
    reopened = session_manager.SessionManager().get_session("continuity")
    assert len(reopened.history) == 2
    assert "Do not send any email." in reopened.history[0].content
    assert reopened.history[0].content.count("Preserved (compaction-safe):") == 1
    assert reopened.history[0].metadata["compaction_preserve"] == live[0]["metadata"]["compaction_preserve"]
    assert reopened.history[-1].content == "Continue review."


def test_changed_history_still_refuses_deferred_replacement(manager):
    session, rows = prepare(manager)
    state = {}
    cc._finish_compaction(session, rows, [], rows[:2], rows[2:], "Draft reviewed.",
                          1000, 2000, None, False, state, 2, kind="llm_summary")
    replacement = [ChatMessage("user", "Changed task.")] + session.history[1:]
    manager.replace_messages("continuity", replacement)
    cc.apply_compaction_state(manager.get_session("continuity"), state)
    reopened = session_manager.SessionManager().get_session("continuity")
    assert len(reopened.history) == 3
    assert reopened.history[0].content == "Changed task."


def test_three_compactions_keep_prior_protected_system_rows(manager, monkeypatch):
    prepare(manager)
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 1000)
    monkeypatch.setattr(cc, "estimate_tokens_for", lambda *a: 2000)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "model")

    async def fake_summary(*a, **kw):
        return "Review continues."

    monkeypatch.setattr(cc, "summarize_rows", fake_summary)
    first_preserved = None
    for round_index in range(3):
        for number in range(4):
            manager.add_message("continuity", ChatMessage(
                "assistant" if number % 2 == 0 else "user", f"Review round {round_index}, note {number}."))
        session = manager.get_session("continuity")
        rows = session.get_context_messages()
        cc.annotate_history_positions(session, rows)
        _, _, compacted = asyncio.run(cc.maybe_compact(session, "http://local", "test", rows))
        assert compacted
        reopened = session_manager.SessionManager().get_session("continuity")
        protected = [row for row in reopened.history if row.role == "system"
                     and "Do not send any email." in row.content]
        assert len(protected) == 1
        current = (protected[0].content, protected[0].metadata["compaction_preserve"])
        if first_preserved is None:
            first_preserved = current
        assert current == first_preserved
