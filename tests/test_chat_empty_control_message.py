"""Browser FormData omits blank messages; approvals are still valid controls."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.chat_helpers import coerce_message_and_session


@pytest.mark.parametrize("message", [None, "", "   "])
def test_approval_or_attachment_can_omit_text_in_form_data(message):
    sessions = SimpleNamespace(get_session=lambda sid: object())
    assert coerce_message_and_session(None, message, "s1", sessions, allow_empty=True) == ("", "s1")


@pytest.mark.parametrize("allow_empty", [False, True])
def test_an_empty_control_does_not_bypass_the_session_requirement(allow_empty):
    sessions = SimpleNamespace(get_session=lambda sid: object())
    with pytest.raises(HTTPException) as error:
        coerce_message_and_session(None, None, None, sessions, allow_empty=allow_empty)
    assert error.value.status_code == 400


def test_missing_text_is_still_refused_for_a_normal_send():
    with pytest.raises(HTTPException) as error:
        coerce_message_and_session(None, None, "s1", SimpleNamespace(), allow_empty=False)
    assert error.value.status_code == 400


def test_optional_text_still_reads_a_supplied_json_message():
    sessions = SimpleNamespace(get_session=lambda sid: object())
    assert coerce_message_and_session({"message": "keep this text"}, None, "s1", sessions,
                                      allow_empty=True) == ("keep this text", "s1")


def test_new_message_closes_only_unresolved_tool_cards(monkeypatch):
    import routes.chat_routes as routes
    calls = []
    monkeypatch.setattr(routes, "_mark_tool_approval_resolved",
                        lambda sess, approval_id, decision: calls.append((approval_id, decision)))
    session = SimpleNamespace(history=[SimpleNamespace(metadata={"tool_events": [
        {"ask_user": {"kind": "tool_approval", "approval_id": "old"}},
        {"ask_user": {"kind": "tool_approval", "approval_id": "answered", "resolved": "deny"}},
        {"ask_user": {"kind": "question", "approval_id": "not-a-tool"}},
    ]})])
    routes._supersede_tool_approval_history(session)
    assert calls == [("old", "superseded")]


@pytest.mark.asyncio
async def test_actual_approval_route_accepts_browser_empty_message(monkeypatch):
    import routes.chat_routes as routes
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    from src.tool_capabilities import capabilities_for_action

    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # Most older route tests stub this boundary, hiding the browser failure.
    monkeypatch.setattr(routes, "coerce_message_and_session", coerce_message_and_session)
    pending = routes.tool_approval_store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1",
        tool_name="project_objectives", content='{"action":"list"}', workspace=None,
        external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("project_objectives", '{"action":"list"}'),
    )
    request = _RouteRequest("chat")
    request._form.update({"message": "", "compare_mode": "false",
                          "tool_approval_id": pending.approval_id,
                          "tool_approval_decision": "approve"})
    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass
    assert captured["exact_approval"].pending == pending
