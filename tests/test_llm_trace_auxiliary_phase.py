import asyncio
import json

import pytest

from src import llm_trace as trace, llm_core, context_compactor as cc, agent_loop as al


@pytest.fixture
def traces(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "_traces_dir", lambda: str(tmp_path))
    monkeypatch.setattr(trace, "tracing_enabled", lambda: True)
    trace._SEQ_CACHE.clear()
    yield
    trace.flush_for_tests()


def record(model="fixture"):
    trace.record_call(session_id="own", model=model, request={"messages": []},
                      usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})


def reopened():
    trace.flush_for_tests()
    trace._SEQ_CACHE.clear()
    rows = trace.list_calls("own")
    return rows, [trace.get_call("own", row["seq"]) for row in rows]


def test_nested_reset_exception_unknown_and_closed_labels(traces):
    record("unknown")
    with trace.call_phase("compaction"):
        record("outer")
        with pytest.raises(ValueError):
            with trace.call_phase("recovery", step=2):
                record("inner")
                raise ValueError("fixture")
        record("outer-again")
        with trace.call_phase("not-a-phase", step=3):
            record("invalid")
    record("after")
    rows, full = reopened()
    assert [r.get("phase") for r in rows] == [None,"compaction","recovery","compaction",None,None]
    assert [r.get("step") for r in rows] == [None,None,2,None,None,None]
    assert [r.get("phase") for r in full] == [r.get("phase") for r in rows]
    assert all(r["usage"]["total_tokens"] == 10 for r in rows)


@pytest.mark.asyncio
async def test_concurrent_contexts_and_cancel_do_not_leak(traces):
    entered = asyncio.Event()
    async def cancelled():
        with trace.call_phase("recovery", step=3):
            record("cancelled")
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(cancelled())
    await entered.wait()
    with trace.call_phase("compaction"):
        record("parallel")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record("unscoped")
    rows, _ = reopened()
    assert {r["model"]: (r.get("phase"),r.get("step")) for r in rows} == {
        "cancelled":("recovery",3), "parallel":("compaction",None), "unscoped":(None,None)}


@pytest.mark.parametrize("step", [None,1,4,True,"2"])
def test_invalid_step_not_persisted(traces, step):
    with trace.call_phase("recovery", step=step):
        record()
    rows, full = reopened()
    assert rows[0]["phase"] == "recovery"
    assert "step" not in rows[0] and "step" not in full[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", [None,2,3])
async def test_real_recovery_helper_and_stream_trace(traces, monkeypatch, step):
    async def source(*args, **kwargs):
        yield 'data: '+json.dumps({"type":"usage","data":{"input_tokens":7,"output_tokens":3,"total_tokens":10}})+'\n\n'
        yield 'data: '+json.dumps({"delta":"answer"})+'\n\n'
    monkeypatch.setattr(llm_core, "_stream_llm_traced_source", source)
    result = await al._recovery_step_completion("https://fixture.invalid", "fixture", {}, [], .2, 100, {}, "own", 5, _trace_step=step)
    assert result[0] == "answer"
    record("after")
    rows, _ = reopened()
    assert rows[0]["phase"] == "recovery" and rows[0].get("step") == step
    assert rows[0]["usage"]["total_tokens"] == 10
    assert "phase" not in rows[1]


@pytest.mark.asyncio
async def test_compactor_scopes_actual_inference_only(traces, monkeypatch):
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *a, **k: (None,None,None))
    monkeypatch.setattr("src.privacy_policy.assert_outbound", lambda *a, **k: None)
    async def infer(*args, **kwargs):
        record("summary")
        return "summary"
    monkeypatch.setattr(cc, "llm_call_async", infer)
    result = await cc.summarize_rows([{"role":"user","content":"fixture"}], endpoint_url="https://fixture.invalid",model="fixture",session_id="own")
    assert "summary" in result
    record("after")
    rows, _ = reopened()
    assert rows[0]["phase"] == "compaction" and "step" not in rows[0]
    assert "phase" not in rows[1]


@pytest.mark.asyncio
async def test_real_ladder_explicitly_labels_both_steps_without_observer(traces, monkeypatch):
    monkeypatch.setattr(al, "_assemble_prompt", lambda *a, **k: "fixture")
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", lambda *a, **k: ("https://utility.invalid", "utility", {}))
    async def source(url, model, *args, **kwargs):
        yield 'data: '+json.dumps({"delta":"" if model == "primary" else "Utility answer."})+'\n\n'
    monkeypatch.setattr(llm_core, "_stream_llm_traced_source", source)
    events = [item async for item in al._recovery_ladder(reason="fixture",endpoint_url="https://primary.invalid",model="primary",headers={},messages=[{"role":"user","content":"fixture"}],temperature=.7,max_tokens=100,gen_overrides=None,session_id="own",owner=None,agent_stream_timeout=5)]
    assert events[-1][1]["ok"]
    rows, _ = reopened()
    assert [(r["model"],r["phase"],r["step"]) for r in rows] == [("primary","recovery",2),("utility","recovery",3)]


@pytest.mark.asyncio
async def test_extractive_compaction_does_not_invent_trace(traces, monkeypatch):
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "estimate_tokens_for", lambda *a: 1000)
    monkeypatch.setattr(cc, "refresh_compaction_approvals", lambda rows: rows)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "extract")
    async def forbidden(*a, **k):
        pytest.fail("extractive compaction must not infer")
    monkeypatch.setattr(cc, "llm_call_async", forbidden)
    messages=[{"role":"user" if i%2==0 else "assistant","content":"fixture"} for i in range(6)]
    await cc.maybe_compact({"id":"own"},"https://fixture.invalid","fixture",messages,persist=False)
    rows, _ = reopened()
    assert rows == []
