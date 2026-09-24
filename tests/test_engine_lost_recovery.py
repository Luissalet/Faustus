"""Coverage for the "engine lost mid-round" recovery (24/25-09-2026): a
managed local llama.cpp engine dying PARTWAY THROUGH a round's generation —
after deltas were already streamed to the client — used to end the whole
agent turn with a raw "Model request failed (HTTP 502)" terminal_error, even
though `src.engine_swap` could have restarted it. `src.llm_core`/
`src.engine_swap` already recover a connect-phase failure (before any
output); this covers the mid-generation case one layer up, in the agent
harness.

- `_looks_like_engine_lost` classifies transport statuses (502/503/504) and
  connection-loss wording, and does not misfire on ordinary provider errors
  (bad request, rate limit).
- `stream_agent_loop` restarts the engine and redoes the round exactly once
  per turn when the round's stream error looks like a lost engine and the
  round's endpoint maps to a managed engine; the partial round text is
  discarded the same way the degenerate-output retry discards its garbage.
- A failed restart, or an endpoint that isn't a managed engine, falls through
  to the existing terminal_error behaviour unchanged.
"""
import asyncio
import json

import src.llm_core as llm_core
import src.agent_loop as al


# ── helper classification ───────────────────────────────────────────────────

def test_looks_like_engine_lost_matches_transport_statuses():
    assert llm_core._looks_like_engine_lost({"error": "boom"}, 502)
    assert llm_core._looks_like_engine_lost({"error": "boom"}, 503)
    assert llm_core._looks_like_engine_lost({"error": "boom"}, 504)
    assert not llm_core._looks_like_engine_lost({"error": "bad request"}, 400)


def test_looks_like_engine_lost_matches_connection_loss_wording():
    assert llm_core._looks_like_engine_lost(
        {"error": "Server disconnected without sending a response"}
    )
    assert llm_core._looks_like_engine_lost(
        {"error": "Cannot reach 127.0.0.1:8081: [Errno 111] Connection refused"}
    )
    assert llm_core._looks_like_engine_lost(
        {"error": "peer closed connection without sending complete message body "
                   "(incomplete chunked read)"}
    )
    assert llm_core._looks_like_engine_lost(
        {"error": "httpx.RemoteProtocolError: garbage"}
    )
    assert llm_core._looks_like_engine_lost({"error": "Connection reset by peer"})
    assert llm_core._looks_like_engine_lost({"error": "connection refused"})
    assert llm_core._looks_like_engine_lost({"error": "the connection closed unexpectedly"})


def test_looks_like_engine_lost_false_for_unrelated_or_malformed_errors():
    assert not llm_core._looks_like_engine_lost(None, 502)
    assert not llm_core._looks_like_engine_lost({"error": "rate limited"}, 429)
    assert not llm_core._looks_like_engine_lost({"error": "invalid api key"}, 401)
    assert not llm_core._looks_like_engine_lost({"error": "boom", "status": "nope"})


def test_looks_like_engine_lost_reads_status_from_error_data_when_not_passed():
    assert llm_core._looks_like_engine_lost({"error": "boom", "status": 503})
    assert not llm_core._looks_like_engine_lost({"error": "boom", "status": 429})


# ── agent-loop redo ─────────────────────────────────────────────────────────

def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _engine_lost_error_chunk(status=502, message="Server disconnected without sending a response"):
    payload = {"status": status, "error": message, "partial": True}
    return f"event: error\ndata: {json.dumps(payload)}\n\n"


def test_engine_lost_mid_round_restarts_engine_and_redoes_the_round(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al.engine_swap, "restartable_engine_for_url", lambda url: {"id": "eng1"})
    recovered = {"called_with": None}

    async def _fake_recover(url):
        recovered["called_with"] = url
        return True
    monkeypatch.setattr(al.engine_swap, "recover_after_connect_failure", _fake_recover)

    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        if round_no == 1:
            yield f'data: {json.dumps({"delta": "Partial text before the engine died"})}\n\n'
            yield _engine_lost_error_chunk()
        else:
            yield f'data: {json.dumps({"delta": "Full answer after restart."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "local-model",
        [{"role": "user", "content": "Do a long multi-step task"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    assert round_no == 2
    assert recovered["called_with"] == "http://127.0.0.1:8081/v1"
    notice = next(
        (e for e in events
         if e.get("type") == "harness_check" and e.get("reason") == "engine_lost_recovered"),
        None,
    )
    assert notice is not None, events
    assert "restarting" in notice.get("message", "").lower()

    replace_events = [e for e in events if e.get("type") == "response_replace"]
    assert replace_events, events
    # The dead round's partial text must be discarded before the redo, same
    # as the degenerate-output retry discards its collapsed output.
    assert "Partial text before the engine died" not in replace_events[0].get("text", "")

    joined = "".join(raw_chunks)
    assert "Full answer after restart." in joined
    assert not any(c.startswith("event: error") for c in raw_chunks), raw_chunks
    assert not any(e.get("type") == "agent_terminal" for e in events), events


def test_engine_lost_recovery_is_one_shot_per_turn(monkeypatch, tmp_path):
    """A second mid-round engine death in the same turn is not retried again
    — it falls through to the normal terminal_error, same one-shot budget as
    the degenerate-output retry."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al.engine_swap, "restartable_engine_for_url", lambda url: {"id": "eng1"})
    recover_calls = []

    async def _fake_recover(url):
        recover_calls.append(url)
        return True
    monkeypatch.setattr(al.engine_swap, "recover_after_connect_failure", _fake_recover)

    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        yield f'data: {json.dumps({"delta": f"partial {round_no}"})}\n\n'
        yield _engine_lost_error_chunk()

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "local-model",
        [{"role": "user", "content": "Do a long multi-step task"}],
        max_rounds=5,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    assert round_no == 2  # round 1: recovered once. round 2: dies again, not retried again.
    assert len(recover_calls) == 1
    terminal = next(e for e in events if e.get("type") == "agent_terminal")
    assert terminal["data"]["failed"] is True
    assert "502" in terminal["data"]["failure"]["message"]


def test_engine_lost_recovery_falls_through_when_restart_fails(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al.engine_swap, "restartable_engine_for_url", lambda url: {"id": "eng1"})

    async def _fake_recover(url):
        return False
    monkeypatch.setattr(al.engine_swap, "recover_after_connect_failure", _fake_recover)

    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        yield f'data: {json.dumps({"delta": "partial"})}\n\n'
        yield _engine_lost_error_chunk()

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "local-model",
        [{"role": "user", "content": "Do a task"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    assert round_no == 1  # no redo — the restart itself failed
    # The notice is shown as soon as recovery is attempted (before the
    # outcome is known); a failed restart falls through to the ordinary
    # terminal_error instead of redoing the round.
    assert any(
        e.get("type") == "harness_check" and e.get("reason") == "engine_lost_recovered"
        for e in events
    ), events
    assert not any(e.get("type") == "response_replace" for e in events), events
    terminal = next(e for e in events if e.get("type") == "agent_terminal")
    assert terminal["data"]["failed"] is True
    assert "502" in terminal["data"]["failure"]["message"]


def test_engine_lost_pattern_is_ignored_for_a_non_managed_endpoint(monkeypatch, tmp_path):
    """A transport failure that LOOKS like a lost engine, on an endpoint that
    is not a managed local engine (e.g. a remote/API endpoint), must not
    trigger a restart attempt at all — behaviour is unchanged from before
    this feature."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al.engine_swap, "restartable_engine_for_url", lambda url: None)
    recover_calls = []

    async def _fake_recover(url):
        recover_calls.append(url)
        return True
    monkeypatch.setattr(al.engine_swap, "recover_after_connect_failure", _fake_recover)

    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        yield f'data: {json.dumps({"delta": "partial"})}\n\n'
        yield _engine_lost_error_chunk()

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "https://remote-api.internal/v1", "remote-model",
        [{"role": "user", "content": "Do a task"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    assert round_no == 1
    assert not recover_calls
    terminal = next(e for e in events if e.get("type") == "agent_terminal")
    assert terminal["data"]["failed"] is True
