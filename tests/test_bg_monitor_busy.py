"""BUG 6 — the background-job follow-up never marked itself busy.

`_run_followup` yields to a live turn (`agent_runs.is_active(sess.id)`) but
never announced ITSELF, so a live turn — or another follow-up — could start on
the same session at the same time. Both agent loops read
`get_context_messages()`, both `add_message()` + `save_sessions()`: interleaved
messages and tools executed twice. The code's own comment describes exactly
this failure.

`agent_runs.mark_busy()` already exists (subagent_tools uses it for worker
chats); the follow-up now takes it for the length of the run, in a try/finally,
and the deferral check consults it too.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src import agent_runs, bg_monitor


@pytest.fixture(autouse=True)
def _clean():
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL_BUSY.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL_BUSY.clear()


def _session(sid="s1"):
    return SimpleNamespace(id=sid, endpoint_url="http://example.test", model="m",
                           headers=None, context_length=0, owner=None,
                           get_context_messages=lambda: [])


class _SM:
    def __init__(self, sess):
        self._sess = sess
        self.added = []

    def get_session(self, sid):
        return self._sess if self._sess and sid == self._sess.id else None

    def add_message(self, sid, msg):
        self.added.append((sid, msg))

    def save_sessions(self):
        pass


def _wire(monkeypatch, sm, drain):
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm, raising=False)
    monkeypatch.setattr(bg_monitor.bg_jobs, "result_text", lambda rec: "job output", raising=False)
    monkeypatch.setattr(bg_monitor, "_drain_agent", drain)


def test_followup_marks_the_session_busy_while_it_runs(monkeypatch):
    sess = _session()
    sm = _SM(sess)
    seen = {}

    async def _drain(s, messages):
        seen["busy_during"] = s.id in agent_runs.active_session_ids()
        return "continued", []
    _wire(monkeypatch, sm, _drain)

    assert asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": "s1"})) is True
    assert seen["busy_during"] is True, "the follow-up ran without marking the session busy"
    assert agent_runs.active_session_ids() == [], "the busy mark outlived the follow-up"
    assert sm.added and sm.added[0][0] == "s1"


def test_busy_is_cleared_when_the_followup_raises(monkeypatch):
    sess = _session()
    sm = _SM(sess)

    async def _drain(s, messages):
        raise RuntimeError("model exploded")
    _wire(monkeypatch, sm, _drain)

    with pytest.raises(RuntimeError):
        asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": "s1"}))
    assert agent_runs.active_session_ids() == [], "a failed follow-up left the session marked busy"


def test_followup_defers_while_the_session_is_already_busy(monkeypatch):
    """Two follow-ups (or a sub-agent worker) on the same session must not run
    concurrently either — the deferral must see the external busy mark, not
    only detached runs."""
    sess = _session()
    sm = _SM(sess)
    ran = []

    async def _drain(s, messages):
        ran.append(1)
        return "x", []
    _wire(monkeypatch, sm, _drain)

    agent_runs.mark_busy("s1")
    assert asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": "s1"})) is False
    assert ran == [], "the follow-up wrote into a session that was already busy"
    assert sm.added == []


def test_missing_session_is_not_left_marked_busy(monkeypatch):
    sm = _SM(None)

    async def _drain(s, messages):    # pragma: no cover - must not be reached
        raise AssertionError("should not run")
    _wire(monkeypatch, sm, _drain)

    assert asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": "gone"})) is True
    assert agent_runs.active_session_ids() == []
