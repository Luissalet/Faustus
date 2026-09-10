"""OBS-01 — call_id on tool_start/tool_progress/tool_output (and the
persisted tool_event), reusing the id the provider (or the assembler ahead
of it) already gave the call rather than inventing a parallel one — src/
agent_loop.py's two tool-execution sites (the main per-round loop and the
pre-approved single-tool continuation).

Reuses test_agent_harness_loop.py's scripted-stream harness (real loop body,
fake LLM stream / tool exec) — the same pattern
test_agent_harness_loop_stepcap.py already imports it for.
"""
import json

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events, _native_call_stream, _patch_common, _scripted_stream


def _run(monkeypatch, workspace, user="do something in the repo", max_rounds=6,
         relevant_tools=frozenset({"read_file", "edit_file", "glob", "bash"})):
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user}],
        max_rounds=max_rounds,
        relevant_tools=set(relevant_tools),
        workspace=workspace,
    )
    return _events(_collect(gen))


def test_a_fenced_tool_call_gets_a_stable_synthetic_call_id(tmp_path, monkeypatch):
    """No native provider id (a fenced/local-model call) -> the SAME
    fallback id `_append_tool_results` already used for the follow-up
    message (`call_{round}_{index}`), so tool_start/tool_progress/
    tool_output/the persisted tool_event all agree on one call's identity.
    FAILS without the change: none of these events carried `call_id` at
    all before this lot."""
    _patch_common(monkeypatch)
    _scripted_stream(monkeypatch, [
        ('```bash\necho hi\n```', "tool_calls"),
        ("Done. No files were changed.", "stop"),
    ])
    events = _run(monkeypatch, str(tmp_path), user="Run echo hi")

    starts = [e for e in events if e.get("type") == "tool_start"]
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert len(starts) == 1 and len(outputs) == 1
    assert starts[0]["call_id"] == "call_1_0"
    assert outputs[0]["call_id"] == "call_1_0"


def test_two_parallel_native_calls_with_no_provider_id_get_distinct_call_ids(tmp_path, monkeypatch):
    """The regression a single shared/omitted call_id would cause: two
    results from the SAME round becoming indistinguishable to anything
    correlating by call_id. Neither call carries a provider `id` here (some
    OpenAI-compatible gateways omit it on every delta — see
    src/tool_call_assembler.py's own `resolved_id` fallback), so both fall
    back to the synthetic `call_{round}_{index}` scheme, which must still
    tell them apart by index."""
    _patch_common(monkeypatch)
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    _native_call_stream(monkeypatch, [
        [
            {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            {"name": "read_file", "arguments": json.dumps({"path": "b.py"})},
        ],
        "Read both. Nothing changed.",
    ])
    events = _run(monkeypatch, str(tmp_path), user="Read a.py and b.py")

    starts = [e for e in events if e.get("type") == "tool_start"]
    assert len(starts) == 2
    ids = [e["call_id"] for e in starts]
    assert ids == ["call_1_0", "call_1_1"]
    assert len(set(ids)) == 2

    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert [e["call_id"] for e in outputs] == ids


def test_a_native_tool_call_reuses_the_providers_own_id(tmp_path, monkeypatch):
    """A native function-calling provider's own `id` (e.g. Anthropic/OpenAI's
    `toolu_...`/`call_...`) is reused verbatim as call_id, not replaced by a
    synthetic one — the same id `_append_tool_results` already echoes back
    as `tool_call_id` in the follow-up message, so the two never disagree
    about what a call is called."""
    _patch_common(monkeypatch)
    _native_call_stream(monkeypatch, [
        [{"id": "toolu_abc123", "name": "read_file", "arguments": json.dumps({"path": "server.py"})}],
        "Read it. Nothing changed.",
    ])
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    events = _run(monkeypatch, str(tmp_path), user="Read server.py")

    starts = [e for e in events if e.get("type") == "tool_start"]
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert starts and starts[0]["call_id"] == "toolu_abc123"
    assert outputs and outputs[0]["call_id"] == "toolu_abc123"


def test_the_persisted_tool_event_record_carries_call_id_too():
    """metadata.tool_events[i] (the history-reload record) is built from the
    SAME `_call_id` local the live tool_start/tool_progress/tool_output
    events above are proven to carry — it is not independently observable
    through the SSE stream this harness drives (it is handed to the caller
    through chat_routes.py's own session-save path, not yielded), so this
    pins the source line directly rather than claiming end-to-end coverage
    it does not have. See "Tests" in the lot report."""
    src = open("src/agent_loop.py", encoding="utf-8").read()
    assert '"call_id": _call_id,\n            }' in src, (
        "the persisted tool_event dict must carry the same call_id as the live events"
    )
    assert '"call_id": _approved_call_id,' in src, (
        "the pre-approved continuation's persisted tool_event must carry call_id too"
    )
