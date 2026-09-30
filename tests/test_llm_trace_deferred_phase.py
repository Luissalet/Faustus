import asyncio
import json

import pytest

from src import agent_loop as al, llm_core as lc, llm_trace as tr


@pytest.fixture
def traces(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_traces_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tr, "tracing_enabled", lambda: True)
    tr._SEQ_CACHE.clear()
    yield
    tr.flush_for_tests()


def records():
    tr.flush_for_tests()
    tr._SEQ_CACHE.clear()
    rows = tr.list_calls("own")
    return rows, [tr.get_call("own", r["seq"]) for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", [2,3])
async def test_error_recovery_deferred_asyncgen_finalizer_keeps_phase(traces, monkeypatch, step):
    async def source(*a, **k):
        yield 'data: '+json.dumps({"type":"usage","data":{"input_tokens":7,"output_tokens":3}})+'\n\n'
        yield 'event: error\ndata: {"error":"fixture","status":503}\n\n'
    monkeypatch.setattr(lc, "_stream_llm_traced_source", source)
    result = await al._recovery_step_completion("https://fixture.invalid","fixture",{},[],.2,100,{},"own",5,_trace_step=step)
    assert result[3]
    await asyncio.get_running_loop().shutdown_asyncgens()
    rows, full = records()
    assert len(rows) == 1
    assert (rows[0]["phase"], rows[0]["step"]) == ("recovery",step)
    assert (full[0]["phase"], full[0]["step"]) == ("recovery",step)
    assert rows[0]["usage"] == {"input_tokens":7,"output_tokens":3}


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [None,"recovery"])
async def test_early_close_under_different_context_preserves_entry(traces, monkeypatch, initial):
    async def source(*a, **k):
        yield 'data: {"delta":"partial"}\n\n'
        yield 'data: {"delta":"unused"}\n\n'
    monkeypatch.setattr(lc, "_stream_llm_traced_source", source)
    with tr.call_phase(initial, step=2):
        stream = lc.stream_llm("https://fixture.invalid","fixture",[],session_id="own")
        await anext(stream)
    with tr.call_phase("compaction"):
        await stream.aclose()
    rows, full = records()
    assert rows[0].get("phase") == initial
    assert full[0].get("phase") == initial
    assert full[0]["response_text"] == "partial"
    assert rows[0].get("step") == (2 if initial else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [None,"compaction"])
async def test_nonstream_wrapper_captures_entry_before_impl_context_change(traces, monkeypatch, initial):
    tokens = []
    async def impl(*a, **kwargs):
        tokens.append(tr._CURRENT_CALL_PHASE.set(("recovery",3)))
        kwargs["_on_observed_usage"]({"input_tokens":7})
        return "answer"
    monkeypatch.setattr(lc, "_llm_call_async_impl", impl)
    with tr.call_phase(initial):
        try:
            assert await lc.llm_call_async("https://fixture.invalid","fixture",[],session_id="own") == "answer"
        finally:
            for token in reversed(tokens):
                tr._CURRENT_CALL_PHASE.reset(token)
    rows, _ = records()
    assert rows[0].get("phase") == initial
    assert "step" not in rows[0]
    assert rows[0]["usage"]["input_tokens"] == 7


def test_record_default_legacy_context_and_explicit_unknown_differ(traces):
    unknown = tr._capture_call_phase()
    with tr.call_phase("recovery", step=3):
        tr.record_call(session_id="own",model="legacy")
        tr.record_call(session_id="own",model="captured-unknown",_phase_snapshot=unknown)
    rows, _ = records()
    assert rows[0]["phase"] == "recovery" and rows[0]["step"] == 3
    assert "phase" not in rows[1] and "step" not in rows[1]
