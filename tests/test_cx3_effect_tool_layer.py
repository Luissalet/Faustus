"""H03/H04 at the tool layer: admission before dispatch, attempt id and effect
certainty on every result, unknown instead of failed after dispatch."""
import asyncio

import pytest

from src import effect_outbox as eo
from src import effect_tools, tool_execution, tool_presentation
from src.agent_tools import ToolBlock
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, format_tool_result
from src.tool_result import effect_certainty


@pytest.fixture(autouse=True)
def outbox(tmp_path):
    eo.set_db_path(str(tmp_path / "effects.sqlite3"))
    yield
    eo.set_db_path(None)


async def invoke(name="send_email", content='{"to":"a@example.invalid","body":"x"}', call_id="c1", run_id=None):
    return await tool_execution.execute_tool_block(
        ToolBlock(name, content), session_id="s1", owner="alice", call_id=call_id,
        security_context=NO_TOOL_SECURITY_CONTEXT)


def _impl(monkeypatch, fn):
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fn)


@pytest.mark.asyncio
async def test_describe_call_covers_the_non_repeatable_routes():
    d = effect_tools.describe_call
    assert d("send_email", '{"to":"a@b.c"}').kind == "email.send_email"
    assert d("mcp__email__reply_to_email", "{}").transport == "joined"
    assert d("whatsapp_send", '{"to":"1"}').kind == "whatsapp.send"
    assert d("api_call", '{"integration":"crm","method":"POST","path":"/x"}').kind == "http.post"
    assert d("api_call", '{"integration":"crm","method":"GET","path":"/x"}') is None
    opaque = d("mcp__tracker__create_issue", "{}")
    assert opaque.transport == "opaque" and opaque.kind == "connector.tracker"
    assert d("mcp__tracker__list_issues", "{}") is None
    assert d("mcp__memory__create_note", "{}") is None  # built-in server: local state
    assert d("read_file", "{}") is None


@pytest.mark.asyncio
async def test_intent_is_committed_before_the_handler_runs(monkeypatch):
    seen = {}

    async def handler(block, **kw):
        ctx = eo.current_context()
        row = eo.get(ctx.admission_id)
        seen["state"] = row["state"]
        seen["owner"] = row["owner"]
        return "ok", {"status": "succeeded", "output": "sent", "exit_code": 0}

    _impl(monkeypatch, handler)
    _, result = await invoke()
    assert seen == {"state": "admitted", "owner": "alice"}
    assert result["effect_certainty"] in ("confirmed", "unknown")  # settled by the tool layer, not assumed
    rows = eo.list_effects(owner="alice")
    assert len(rows) == 1 and rows[0]["call_id"] == "c1"
    assert result["attempt_id"] == rows[0]["attempt_id"]


@pytest.mark.asyncio
async def test_intent_not_persisted_means_the_handler_is_never_called(monkeypatch):
    called = []

    async def handler(*a, **k):
        called.append(1)
        return "x", {}

    _impl(monkeypatch, handler)
    monkeypatch.setattr(eo, "get", lambda *a, **k: None)
    _, result = await invoke()
    assert called == []
    assert result["status"] == "failed" and result["effect_not_dispatched"] is True
    assert result["effect_certainty"] == "none"
    assert result["attempt_id"].startswith("att_")


@pytest.mark.asyncio
async def test_unwritable_outbox_sends_nothing(monkeypatch, tmp_path):
    called = []

    async def handler(*a, **k):
        called.append(1)
        return "x", {}

    _impl(monkeypatch, handler)
    eo.set_db_path(str(tmp_path / "missing" / "deep" / "x" / "effects.sqlite3"))
    monkeypatch.setattr(eo, "_db", lambda: (_ for _ in ()).throw(eo.sqlite3.OperationalError("disk I/O error")))
    _, result = await invoke()
    assert called == [] and result["effect_not_dispatched"] is True


@pytest.mark.asyncio
async def test_same_call_twice_is_not_dispatched_twice(monkeypatch):
    calls = []

    async def handler(block, **kw):
        calls.append(1)
        ctx = eo.current_context()
        eo.begin_dispatch(ctx.admission_id)
        eo.settle(ctx.admission_id, "succeeded")
        return "ok", {"status": "succeeded", "output": "sent", "exit_code": 0}

    _impl(monkeypatch, handler)
    from src.run_causality import bind_run, reset_run
    token = bind_run("s1", "run-1")
    try:
        await invoke(call_id="same")
        _, second = await invoke(call_id="same")
    finally:
        reset_run(token)
    assert calls == [1]
    assert second["deduplicated"] is True


@pytest.mark.asyncio
async def test_exception_after_dispatch_is_unknown_not_failed(monkeypatch):
    async def handler(block, **kw):
        eo.begin_dispatch(eo.current_context().admission_id)
        raise TimeoutError("read timed out after the request body was sent")

    _impl(monkeypatch, handler)
    _, result = await invoke()
    assert result["status"] == "outcome_unknown" and result["effect_certainty"] == "unknown"
    assert result["uncertainty"]["reconcile_action"]
    row = eo.list_effects(owner="alice")[0]
    assert row["state"] == "outcome_unknown" and row["effect_certainty"] == "unknown"
    text = format_tool_result("send", result, tool="send_email")
    assert "Effect outcome: UNKNOWN" in text and "Do not repeat" in text


@pytest.mark.asyncio
async def test_exception_before_dispatch_is_a_certain_no_effect(monkeypatch):
    async def handler(block, **kw):
        raise ValueError("bad recipient format")

    _impl(monkeypatch, handler)
    with pytest.raises(ValueError):  # unchanged behaviour: the exception propagates
        await invoke()
    row = eo.list_effects(owner="alice")[0]
    assert row["state"] in ("cancelled", "failed_before_effect") and row["effect_certainty"] == "none"


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_keeps_the_uncertainty(monkeypatch):
    async def handler(block, **kw):
        eo.begin_dispatch(eo.current_context().admission_id)
        raise asyncio.CancelledError()

    _impl(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await invoke()
    row = eo.list_effects(owner="alice")[0]
    assert row["effect_certainty"] == "unknown"


@pytest.mark.asyncio
async def test_opaque_connector_write_is_marked_dispatching_before_the_handler(monkeypatch):
    seen = {}

    async def handler(block, **kw):
        seen["state"] = eo.get(eo.current_context().admission_id)["state"]
        return "err", {"status": "failed", "error": "connection reset", "exit_code": 1}

    _impl(monkeypatch, handler)
    _, result = await invoke("mcp__tracker__create_issue", '{"title":"t"}')
    assert seen["state"] == "dispatching"
    # an error with no explicit "not dispatched" marker proves nothing about the remote side
    assert result["effect_certainty"] == "unknown"
    assert eo.list_effects(owner="alice")[0]["state"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_explicit_not_dispatched_marker_is_certain_failure(monkeypatch):
    async def handler(block, **kw):
        return "err", {"status": "failed", "error": "auth missing", "effect_not_dispatched": True, "exit_code": 1}

    _impl(monkeypatch, handler)
    _, result = await invoke("mcp__tracker__create_issue", '{"title":"t"}')
    assert result["effect_certainty"] == "none"
    assert eo.list_effects(owner="alice")[0]["state"] == "failed_before_effect"


@pytest.mark.asyncio
async def test_reads_are_not_admitted_and_certainty_is_none(monkeypatch):
    async def handler(block, **kw):
        return "ok", {"status": "succeeded", "output": "x", "exit_code": 0}

    _impl(monkeypatch, handler)
    _, result = await invoke("read_file", '{"path":"a.txt"}')
    assert eo.list_effects(owner="alice") == []
    assert result["effect_certainty"] == "none" and result["attempt_id"].startswith("att_")


def test_certainty_rules():
    from src.tool_result import normalize_tool_result
    for raw, expected in (({"status": "outcome_unknown", "outcome_unknown": True}, "unknown"),
                          ({"error": "boom", "exit_code": 1}, "unknown"),
                          ({"error": "refused", "effect_not_dispatched": True, "exit_code": 1}, "none"),
                          ({"output": "ok", "exit_code": 0}, "confirmed")):
        typed = normalize_tool_result(raw, call_id="c")
        assert effect_certainty(typed, effect_class="external", raw=raw) == expected, raw
    typed = normalize_tool_result({"error": "x", "exit_code": 1}, call_id="c")
    assert effect_certainty(typed, effect_class="read", raw={"error": "x"}) == "none"


def test_attempt_and_certainty_reach_the_ui_and_history_fields():
    fields = tool_presentation.tool_result_fields(
        {"status": "failed", "error": "x", "attempt_id": "att_1", "effect_certainty": "unknown", "effect_id": "eff_1"})
    assert fields["attempt_id"] == "att_1" and fields["effect_certainty"] == "unknown"
    assert fields["effect_id"] == "eff_1"


def test_mail_link_is_passed_to_the_mail_server_only_for_send_tools():
    ctx = eo.EffectContext(owner="alice", admission_id="eff_9", attempt_id="att_9", tool="send_email")
    token = eo.bind_context(ctx)
    try:
        linked = tool_execution._with_effect_link("mcp__email__send_email", {"to": "x"})
        assert linked["_faustus_effect"]["admission_id"] == "eff_9"
        plain = tool_execution._with_effect_link("mcp__email__list_emails", {"a": 1})
        assert "_faustus_effect" not in plain
    finally:
        eo.reset_context(token)
