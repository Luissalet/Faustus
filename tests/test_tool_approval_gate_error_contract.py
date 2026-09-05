"""Regression coverage for the approval gate's error contract.

Two bugs, observed together in a live agent-mode run against a slow local
Ollama model:

FAULT A (the disguise) — the previous interface decided *why* a request failed by looking for
the substring ``tool`` (or ``auto``) in the error text, replaced whatever the
server said with "This model doesn't support agent tools", and persisted a
mode switch to localStorage. All three of the approval gate's 409 messages
contain the word "tool", so the exact messages that exist to explain why an
approval was refused were the ones guaranteed to be swallowed.

FAULT B (the cause) — the approval TTL was 10 minutes. Reviewing a diff on a
~1 tok/s model routinely takes longer than that, so the gate 409'd on a
perfectly legitimate click and the approved patch was silently dropped.

The gate now answers with a machine-readable ``code`` next to the human
message, chat.js renders the server's own message, and only the explicit
``model_no_tools`` code may switch modes.
"""

import re
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import routes.chat_routes as chat_routes
from src.tool_approvals import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    ToolApprovalStore,
)
from src.tool_capabilities import capabilities_for_action
from src.tool_security import (
    MODEL_NO_TOOLS_CODE,
    TOOL_APPROVAL_EXPIRED_CODE,
    TOOL_APPROVAL_INVALID_CODE,
    TOOL_APPROVAL_PLAN_MODE_CODE,
    TOOL_APPROVAL_UNAVAILABLE_CODE,
    error_code,
    error_message,
    tool_error_detail,
)

# The gate raises with the constant *names*, so the source-level audit below
# checks for those identifiers rather than their values.
TOOL_APPROVAL_EXPIRED_CODE_NAME = "TOOL_APPROVAL_EXPIRED_CODE"
TOOL_APPROVAL_INVALID_CODE_NAME = "TOOL_APPROVAL_INVALID_CODE"
TOOL_APPROVAL_PLAN_MODE_CODE_NAME = "TOOL_APPROVAL_PLAN_MODE_CODE"
TOOL_APPROVAL_UNAVAILABLE_CODE_NAME = "TOOL_APPROVAL_UNAVAILABLE_CODE"

from test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


_REPO = Path(__file__).resolve().parent.parent
def _seed_approval(monkeypatch, *, tool_name="apply_patch", content='{"patch":"x"}'):
    """Park a real pending approval on the module-level store."""
    return chat_routes.tool_approval_store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name=tool_name,
        content=content,
        workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action(tool_name, content),
    )


async def _drain(response):
    async for _ in response.body_iterator:
        pass


# --------------------------------------------------------------------------
# FAULT B — the 25-minute human review
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_survives_a_twenty_five_minute_human_review(monkeypatch):
    """The live repro: card shown, diff read for ~25 min, then approved.

    With the old 10-minute TTL `peek()` returned None and the gate 409'd, so
    the approved patch was never applied.
    """
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    pending = _seed_approval(monkeypatch)

    request = _RouteRequest("agent")
    request._form.update(
        {
            "tool_approval_id": pending.approval_id,
            "tool_approval_decision": "approve_task",
            "compare_mode": "false",
        }
    )

    created_at = pending.created_at
    monkeypatch.setattr(time, "time", lambda: created_at + 25 * 60)

    response = await endpoint(request)
    await _drain(response)

    assert captured["exact_approval"].pending == pending


def test_default_approval_ttl_covers_a_slow_model_review():
    # A ~1 tok/s local model means the diff sits on screen for tens of
    # minutes. Ten was not enough; the window has to outlast a real read.
    assert DEFAULT_APPROVAL_TTL_SECONDS >= 30 * 60


def test_approval_ttl_is_absolute_not_sliding():
    """The deadline is fixed at creation and nothing may extend it.

    A sliding window would have to be refreshed by the browser, which turns a
    privileged sealed action into something an open tab keeps alive forever.
    """
    store = ToolApprovalStore(ttl_seconds=60)
    pending = store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="printf hi",
        workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf hi"),
    )
    assert pending.expires_at == pytest.approx(pending.created_at + 60)

    # Peeking is user activity; it must not push the deadline out.
    for _ in range(3):
        assert store.peek(pending.approval_id) is not None
    assert store.peek(pending.approval_id).expires_at == pending.expires_at


# --------------------------------------------------------------------------
# The gate's structured error contract
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_approval_is_reported_as_expired_and_rerunnable(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    pending = _seed_approval(monkeypatch)

    request = _RouteRequest("agent")
    request._form.update(
        {
            "tool_approval_id": pending.approval_id,
            "tool_approval_decision": "approve_task",
            "compare_mode": "false",
        }
    )

    created_at = pending.created_at
    monkeypatch.setattr(time, "time", lambda: created_at + DEFAULT_APPROVAL_TTL_SECONDS + 1)

    with pytest.raises(HTTPException) as excinfo:
        await endpoint(request)

    assert excinfo.value.status_code == 409
    assert error_code(excinfo.value.detail) == TOOL_APPROVAL_EXPIRED_CODE
    message = error_message(excinfo.value.detail)
    assert "expired" in message.lower()
    # An expired approval is not a dead end: the message has to name the way out.
    assert "rerun" in message.lower()


@pytest.mark.asyncio
async def test_unknown_approval_keeps_its_original_message(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)

    request = _RouteRequest("agent")
    request._form.update(
        {
            "tool_approval_id": "never-existed",
            "tool_approval_decision": "approve",
            "compare_mode": "false",
        }
    )

    with pytest.raises(HTTPException) as excinfo:
        await endpoint(request)

    assert excinfo.value.status_code == 409
    assert error_code(excinfo.value.detail) == TOOL_APPROVAL_INVALID_CODE
    assert error_message(excinfo.value.detail) == (
        "This tool approval is invalid, expired, or belongs to another thread."
    )


@pytest.mark.asyncio
async def test_plan_mode_refusal_keeps_its_original_message(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    pending = _seed_approval(monkeypatch)

    request = _RouteRequest("agent")
    request._form.update(
        {
            "tool_approval_id": pending.approval_id,
            "tool_approval_decision": "approve",
            "plan_mode": "true",
            "compare_mode": "false",
        }
    )

    with pytest.raises(HTTPException) as excinfo:
        await endpoint(request)

    assert excinfo.value.status_code == 409
    assert error_code(excinfo.value.detail) == TOOL_APPROVAL_PLAN_MODE_CODE
    assert error_message(excinfo.value.detail) == (
        "Tool approvals cannot be consumed while plan mode is active."
    )


@pytest.mark.asyncio
async def test_unconsumable_approval_keeps_its_original_message(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    pending = _seed_approval(monkeypatch)

    # peek() succeeds, consume() refuses — the third 409 the gate can raise.
    monkeypatch.setattr(
        chat_routes.tool_approval_store,
        "consume",
        lambda *args, **kwargs: None,
    )

    request = _RouteRequest("agent")
    request._form.update(
        {
            "tool_approval_id": pending.approval_id,
            "tool_approval_decision": "approve",
            "compare_mode": "false",
        }
    )

    with pytest.raises(HTTPException) as excinfo:
        await endpoint(request)

    assert excinfo.value.status_code == 409
    assert error_code(excinfo.value.detail) == TOOL_APPROVAL_UNAVAILABLE_CODE
    assert error_message(excinfo.value.detail) == (
        "This tool approval could not be consumed."
    )


def test_expired_classification_is_owner_scoped():
    """A leaked opaque id must not tell a stranger the approval ever existed."""
    store = ToolApprovalStore(ttl_seconds=1)
    pending = store.create(
        owner="Alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="printf hi",
        workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf hi"),
    )
    store.expire_now(pending.approval_id)

    assert store.was_expired(pending.approval_id, owner="alice") is True
    assert store.was_expired(pending.approval_id, owner="ALICE ") is True
    assert store.was_expired(pending.approval_id, owner="mallory") is False
    assert store.was_expired("some-other-id", owner="alice") is False


def test_tool_error_detail_round_trips():
    detail = tool_error_detail("some_code", "Some message.")
    assert detail == {"code": "some_code", "message": "Some message."}
    assert error_code(detail) == "some_code"
    assert error_message(detail) == "Some message."
    # Plain-string details (every other raise in the app) still read cleanly.
    assert error_code("plain text") == ""
    assert error_message("plain text") == "plain text"


# --------------------------------------------------------------------------
# FAULT A — the disguise in chat.js
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The two suspects that were NOT the cause. Pinned so the next reader does not
# have to re-derive it, and so a refactor cannot quietly make them real.
# --------------------------------------------------------------------------


def test_stored_owner_is_normalized_exactly_like_the_gate_normalizes_it():
    """Suspect 2 (owner mismatch) was never possible.

    The gate compares against ``str(owner).strip().casefold()``. ``create()``
    runs the same normalization through ``_binding_payload``, so "Admin" and
    "admin" already collapse to the same stored value.
    """
    store = ToolApprovalStore()
    pending = store.create(
        owner="  ADMIN  ",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="printf hi",
        workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf hi"),
    )

    assert pending.owner == "admin"
    assert pending.owner == str("Admin").strip().casefold()
    assert store.consume(
        pending.approval_id,
        decision="approve",
        owner="Admin",
        session_id="session-1",
    ) is not None


def test_chat_stream_only_ever_409s_from_the_approval_gate():
    """Suspect 3 (a still-running previous run) cannot produce a 409 here.

    Every 409 the endpoint can raise is one of the gate's four, so the observed
    ``POST /api/chat_stream 409`` had to come from the approval path.
    """
    source = (_REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    codes = re.findall(r"HTTPException\(\s*409,\s*\n\s*tool_error_detail\(\s*\n\s*(\w+)", source)
    assert sorted(codes) == sorted(
        [
            TOOL_APPROVAL_EXPIRED_CODE_NAME,
            TOOL_APPROVAL_INVALID_CODE_NAME,
            TOOL_APPROVAL_PLAN_MODE_CODE_NAME,
            TOOL_APPROVAL_UNAVAILABLE_CODE_NAME,
        ]
    )
    # No bare-string 409 is left in the file to slip past the code contract.
    assert re.search(r"HTTPException\(\s*409,\s*[\"']", source) is None


def test_no_current_backend_path_claims_a_model_cannot_use_tools():
    """The disguise had no truth behind it — and still has none.

    ``agent_tools_unsupported_reason`` is the one place allowed to say a model
    cannot run agent tools. It says no such thing today, because XML tool
    fences keep every reachable model tool-capable; ``supports_tools = False``
    and the no-native-tools model list only turn off *native* function calls.
    So the client's auto-switch was never reachable from a real server signal,
    which is exactly why it only ever fired on approval-gate errors.
    """
    from src.tool_security import agent_tools_unsupported_reason

    assert agent_tools_unsupported_reason() is None
    assert agent_tools_unsupported_reason(endpoint_supports_tools=False) is None
    assert agent_tools_unsupported_reason(model="gpt-oss:20b") is None
    assert agent_tools_unsupported_reason(
        endpoint_supports_tools=False, model="qwen3.8:27b-q8_0"
    ) is None
    assert MODEL_NO_TOOLS_CODE == "model_no_tools"
