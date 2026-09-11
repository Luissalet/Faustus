"""A tool-approval card whose grant no longer exists is served closed.

Seen live after a 7001 restart: the saved history still carried the card
with no `resolved` mark, the Studio restored it with Approve/Deny, parked
the composer on "answer above", and every decision failed as invalid. The
history route now marks such cards `resolved: "expired"` on the served copy
(`routes/history/history_routes.py::_expire_dead_approval_cards`), without
touching the in-memory history it was built from.
"""
import copy

from routes.history import history_routes as hr
from src import tool_approvals


def _history(resolved=None, kind="tool_approval", approval_id="ap-1"):
    ask = {"kind": kind, "approval_id": approval_id, "question": "Allow?"}
    if resolved:
        ask["resolved"] = resolved
    return [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "…", "metadata": {"tool_events": [
            {"tool": "git_branch", "command": "{}", "output": "Waiting for an exact user approval", "ask_user": ask},
        ]}},
    ]


def test_dead_grant_is_served_as_expired(monkeypatch):
    monkeypatch.setattr(tool_approvals.tool_approval_store, "peek", lambda _id: None)
    src = _history()
    before = copy.deepcopy(src)
    out = hr._expire_dead_approval_cards(src)
    ask = out[1]["metadata"]["tool_events"][0]["ask_user"]
    assert ask["resolved"] == "expired"
    # The in-memory history the route was built from is untouched.
    assert src == before


def test_live_grant_is_left_alone(monkeypatch):
    monkeypatch.setattr(tool_approvals.tool_approval_store, "peek", lambda _id: object())
    src = _history()
    out = hr._expire_dead_approval_cards(src)
    assert "resolved" not in out[1]["metadata"]["tool_events"][0]["ask_user"]
    assert out[1] is src[1]


def test_already_resolved_and_plain_questions_are_untouched(monkeypatch):
    monkeypatch.setattr(tool_approvals.tool_approval_store, "peek", lambda _id: None)
    answered = hr._expire_dead_approval_cards(_history(resolved="deny"))
    assert answered[1]["metadata"]["tool_events"][0]["ask_user"]["resolved"] == "deny"
    question = hr._expire_dead_approval_cards(_history(kind="question"))
    assert "resolved" not in question[1]["metadata"]["tool_events"][0]["ask_user"]


def test_entries_without_tool_events_pass_through(monkeypatch):
    monkeypatch.setattr(tool_approvals.tool_approval_store, "peek", lambda _id: None)
    src = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x", "metadata": {"a": 1}}]
    assert hr._expire_dead_approval_cards(src) == src
