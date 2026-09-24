"""A reasoning loop is recovered by the same model, and a nested local call
from the task that holds the model slot does not wait on itself.

24-09-2026, live on a 27B: its reasoning restated "that doesn't include
'pebbled shore'" three times while recalling a sonnet (web search was
available). The guard stopped it, the step-1 retry looped again, the ladder
skipped the same model and asked the small utility model, and that call
waited thirteen minutes on the local-model lock held by the stream whose
error event was still being handled.
"""
import asyncio
import json

import src.agent_loop as al
import src.llm_core as lc
from tests.test_degenerate_output import _collect, _patch_common, _types


def _loop_error_chunk():
    msg = ("Stopped generation: m started repeating tokens (reasoning loop: the same sentence "
           "came back 3 times ('hmm wait, that doesn't include pebbled shore…')).")
    return ("event: error\n" + "data: " + json.dumps(
        {"status": 502, "text": msg, "error": msg, "error_class": "degenerate_output",
         "fallback_eligible": False}) + "\n\n")


def test_the_retry_after_a_reasoning_loop_carries_a_use_your_tools_note(monkeypatch):
    _patch_common(monkeypatch)
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            yield f'data: {json.dumps({"delta": "thinking", "thinking": True})}\n\n'
            yield _loop_error_chunk()
        else:
            yield f'data: {json.dumps({"delta": "Answer."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "m",
        [{"role": "user", "content": "Which sonnet is this line from? Explain in detail."}],
        max_rounds=3, relevant_tools={"read_file", "web_search"},
    ))
    assert len(seen) == 2
    notes = [m.get("content", "") for m in seen[1] if m.get("_harness_note")]
    assert any("went round in circles" in n and "web_search" in n for n in notes), notes


def test_a_second_reasoning_loop_tries_the_same_model_before_the_utility_model(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda prefix, *a, **k: ("http://127.0.0.1:8082/v1", "utility-model", {})
                        if prefix == "utility" else (None, None, None))
    models = []

    async def _fake_stream(_candidates, messages, **kwargs):
        model = _candidates[0][1]
        models.append(model)
        if len(models) <= 2:
            yield _loop_error_chunk()
        else:
            yield f'data: {json.dumps({"delta": "Same-model answer."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw = _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "m",
        [{"role": "user", "content": "Which sonnet is this line from? Explain in detail."}],
        max_rounds=3, relevant_tools={"read_file"},
    ))
    events = _types(raw)
    assert models[:3] == ["m", "m", "m"], models
    assert "utility-model" not in models
    assert any(e.get("type") == "harness_check" and e.get("step") == 2 for e in events), events


def test_a_nested_local_call_from_the_holding_task_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(lc, "_local_model_gate_enabled", lambda: True)

    async def main():
        async def outer_stream():
            async with lc._local_model_slot("http://127.0.0.1:8081/v1", "big"):
                yield "error-event"
                yield "never"

        gen = outer_stream()
        first = await gen.__anext__()  # suspended inside the slot, lock held
        assert first == "error-event" and lc._LOCAL_MODEL_LOCK.locked()

        async def nested():
            async with lc._local_model_slot("http://127.0.0.1:8082/v1", "small"):
                return "answered"

        async with asyncio.timeout(3):  # same task, as in the agent loop
            out = await nested()
        await gen.aclose()
        assert not lc._LOCAL_MODEL_LOCK.locked()
        return out

    assert asyncio.run(main()) == "answered"


def test_another_task_still_waits_for_the_slot(monkeypatch):
    monkeypatch.setattr(lc, "_local_model_gate_enabled", lambda: True)

    async def main():
        release = asyncio.Event()

        async def holder():
            async with lc._local_model_slot("http://127.0.0.1:8081/v1", "big"):
                await release.wait()

        t = asyncio.create_task(holder())
        await asyncio.sleep(0.05)

        async def other():
            async with lc._local_model_slot("http://127.0.0.1:8081/v1", "big"):
                return "got it"

        o = asyncio.create_task(other())
        await asyncio.sleep(0.2)
        assert not o.done(), "a different task must still wait"
        release.set()
        await t
        return await asyncio.wait_for(o, timeout=3)

    assert asyncio.run(main()) == "got it"


def test_a_recall_loop_in_tool_arguments_gets_the_same_note(monkeypatch):
    """Live, exam run 15: the recall went into a python comment instead."""
    _patch_common(monkeypatch)
    seen = []
    msg = ("Stopped generation: m started repeating tokens (python: tool-call arguments cycle "
           "through 2 sentence template(s) over their last 40 lines).")
    chunk = ("event: error\n" + "data: " + json.dumps(
        {"status": 502, "text": msg, "error": msg, "error_class": "degenerate_output",
         "fallback_eligible": False}) + "\n\n")

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            yield chunk
        else:
            yield f'data: {json.dumps({"delta": "Answer."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "m",
        [{"role": "user", "content": "Which poem is this line from? Explain in detail."}],
        max_rounds=3, relevant_tools={"read_file", "web_search", "python"},
    ))
    assert len(seen) == 2
    notes = [m.get("content", "") for m in seen[1] if m.get("_harness_note")]
    assert any("went round in circles" in n for n in notes), notes
