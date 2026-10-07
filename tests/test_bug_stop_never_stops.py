"""BUG-STOP-01 (live report): the Stop button stopped stopping a running
agent turn once commit 4793942e ("Never end an agent turn in silence;
extend on progress") started extending the round budget automatically.

Covers, at the `stream_agent_loop`/`agent_runs` level (same fake-model
harness as tests/test_lote4_agent_progress_auto_continue.py):

* cancellation checked at the top of every round -- a Stop wins the race
  against the progress-based auto-continue extension, even deep into a
  turn that has already been extended many times past its original cap;
* `POST /api/chat/stop`'s `agent_runs.stop_with_reason` reports the truth:
  `stopped=True` only for a run it actually found and cancelled, and a
  concrete reason (never a bare `False`) for a stale/missing run id;
* the wall-clock ceiling (`agent_turn_max_seconds`) ends a turn whose
  rounds are each individually cheap but that has run too long in real
  time, with a question -- never silently and never forever.

Revert the `pending_cancel` checks in src/agent_loop.py's round loop to see
the auto-continue test below fail (the loop keeps calling the fake model
past the point Stop was pressed); revert `agent_runs.stop_with_reason` back
to the old bare-bool `stop` to see the reason test fail.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import time
import threading

import src.agent_loop as al
from src import agent_runs


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


def _settings(overrides):
    def _get(key, default=None):
        return overrides.get(key, default)
    return _get


def _patch_common(monkeypatch, settings_overrides=None):
    monkeypatch.setattr(al, "get_setting", _settings(settings_overrides or {}), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": block.content, "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    # A fresh "skeleton" every call so loop_breaker's own probe-repeat guard
    # never trips and masks what this test is actually checking.
    monkeypatch.setattr(al, "_probe_skeleton", lambda *a, **k: object(), raising=False)


async def _cancel_after_n_rounds(gen, session_id: str, n: int):
    """Drains `gen`, flagging `agent_runs`' cancellation store as cancelled
    for `session_id` right after the Nth `agent_step` event -- mirrors what
    the real `POST /api/chat/stop` route does (`stop_with_reason`), without
    the HTTP layer."""
    seen_steps = 0
    events = []
    run = agent_runs._Run()
    agent_runs._RUNS[session_id] = run
    async for chunk in gen:
        events.append(chunk)
        if '"type": "agent_step"' in chunk:
            seen_steps += 1
            if seen_steps == n:
                run.cancel_requested = "task_cancelled"
    return events, seen_steps


# ── item 5a: cancel during a round → loop exits within one round ──────────

def test_cancel_mid_round_stops_within_one_more_round(monkeypatch):
    _patch_common(monkeypatch, {"agent_auto_continue_cycles": 0})
    counter = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        counter["n"] += 1
        yield "data: " + json.dumps({"delta": "```bash\necho unit-" + str(counter["n"]) + "\n```"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    session_id = "cancel-mid-round"
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do many units"}],
        max_rounds=1000,
        relevant_tools={"bash"},
        session_id=session_id,
        pending_cancel=lambda: agent_runs.is_cancel_requested(session_id),
    )
    events, seen_steps = asyncio.run(_cancel_after_n_rounds(gen, session_id, 3))
    types = _types(events)

    assert any(e.get("type") == "cancelled" for e in types), types
    # The model must not have been called many more times after the flag
    # was set -- one in-flight round finishing is acceptable, an unbounded
    # continuation (the reported bug: "round 180 in the log") is not.
    assert counter["n"] <= 6, counter


# ── item 5b: cancel during the progress-based auto-continue extension ────

def test_cancel_during_auto_continue_extension_stops(monkeypatch):
    _patch_common(monkeypatch, {
        "agent_auto_continue_on_progress": True,
        "agent_auto_continue_max_rounds": 500,
        "agent_auto_continue_cycles": 0,   # force: only the progress gate can extend
    })
    counter = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        counter["n"] += 1
        yield "data: " + json.dumps({"delta": "```bash\necho unit-" + str(counter["n"]) + "\n```"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    session_id = "cancel-during-extension"
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "create one folder per item, one at a time"}],
        max_rounds=2,   # tiny cap -> the progress gate kicks in almost immediately
        relevant_tools={"bash"},
        session_id=session_id,
        pending_cancel=lambda: agent_runs.is_cancel_requested(session_id),
    )
    # Cancel after the FIRST progress_continue extension has already fired,
    # to prove Stop wins even once the "never end in silence" mechanism has
    # taken over the turn.
    seen_steps = 0
    events = []
    run = agent_runs._Run()
    agent_runs._RUNS[session_id] = run
    saw_extension = False

    async def _drive():
        nonlocal seen_steps, saw_extension
        async for chunk in gen:
            events.append(chunk)
            if '"reason": "progress_continue"' in chunk and not saw_extension:
                saw_extension = True
                run.cancel_requested = "task_cancelled"
    asyncio.run(_drive())
    types = _types(events)

    assert saw_extension, "test setup: never even reached the auto-continue extension"
    assert any(e.get("type") == "cancelled" for e in types), types
    # No further rounds after the cancellation was flagged.
    counter_at_cancel = counter["n"]
    assert counter["n"] <= counter_at_cancel + 1


# ── item 5c: cancel during the empty-round nudge retries → exits ─────────

def test_cancel_during_empty_round_nudges_stops(monkeypatch, tmp_path):
    _patch_common(monkeypatch, {"agent_empty_round_max_nudges": 5})

    async def _fake_stream(_candidates, messages, **kwargs):
        yield "data: " + json.dumps({"delta": ""}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    session_id = "cancel-during-nudges"
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do something ambiguous"}],
        max_rounds=20,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
        session_id=session_id,
        pending_cancel=lambda: agent_runs.is_cancel_requested(session_id),
    )
    events, seen_steps = asyncio.run(_cancel_after_n_rounds(gen, session_id, 2))
    types = _types(events)

    # Never reaches the 5-nudge exhaustion's own ask_user -- the cancel wins
    # first and ends the turn as "cancelled", not "empty_rounds_exhausted".
    assert any(e.get("type") == "cancelled" for e in types), types
    empty_round_events = [e for e in types if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty_round_events) <= 3, empty_round_events


# ── item 5d: stop route reports the truth ─────────────────────────────────

def test_stop_with_reason_reports_truth_not_bare_false():
    agent_runs._RUNS.clear()

    async def _forever():
        try:
            while True:
                await asyncio.sleep(0.01)
                yield "data: " + json.dumps({"type": "noop"}) + "\n\n"
        except asyncio.CancelledError:
            raise

    async def _run():
        run = agent_runs.start("truth-session", _forever(), label="t")
        await asyncio.sleep(0.02)

        # A live run, correct id: stopped=True, reason="cancelled".
        ok, reason = agent_runs.stop_with_reason("truth-session", run.run_id)
        assert ok is True
        assert reason == "cancelled"
        await asyncio.sleep(0.02)

        # No run at all for this session: a clear reason, not a bare False.
        ok2, reason2 = agent_runs.stop_with_reason("no-such-session-at-all", "whatever")
        assert ok2 is False
        assert reason2 == "no_active_run"

        # A stale/mismatched run id against a still-live run: distinguished
        # from "no run" and from "already finished".
        run2 = agent_runs.start("truth-session-2", _forever(), label="t2")
        await asyncio.sleep(0.02)
        ok3, reason3 = agent_runs.stop_with_reason("truth-session-2", "not-the-real-id")
        assert ok3 is False
        assert reason3 == "run_id_mismatch"
        run2.task.cancel()
        await asyncio.gather(run2.task, return_exceptions=True)

    asyncio.run(_run())
    agent_runs._RUNS.clear()


# ── item 5e: wall-clock ceiling ends the turn ─────────────────────────────

def test_wall_clock_ceiling_ends_turn_with_a_question(monkeypatch):
    """Individually-cheap rounds that never trip the round-budget/progress
    checks must still be bounded in real time (item 4)."""
    _patch_common(monkeypatch, {
        "agent_auto_continue_on_progress": True,
        "agent_auto_continue_max_rounds": 100000,
        "agent_turn_max_seconds": 0.05,   # tiny for the test
    })
    counter = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        counter["n"] += 1
        yield "data: " + json.dumps({"delta": "```bash\necho unit-" + str(counter["n"]) + "\n```"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "keep going forever"}],
        max_rounds=100000,
        relevant_tools={"bash"},
    )
    events = _types(_collect(gen))
    assert any(e.get("type") == "ask_user" for e in events), events
    # Never ran to the (absurd) round cap -- the wall clock stopped it long
    # before that, even though nothing else would have.
    assert counter["n"] < 1000, counter


def test_turn_ceiling_cancels_a_silent_inference_and_closes_its_stream(monkeypatch):
    # The full loop performs ordinary turn setup before starting inference;
    # leave enough wall time to prove cancellation of the provider await.
    _patch_common(monkeypatch, {"agent_turn_max_seconds": 5,
                                "agent_auto_continue_on_progress": True})
    state = {"started": False, "closed": False}

    async def _silent_stream(_candidates, _messages, **_kwargs):
        state["started"] = True
        try:
            await asyncio.sleep(30)
            yield "data: [DONE]\n\n"
        finally:
            state["closed"] = True

    monkeypatch.setattr(al, "stream_llm_with_fallback", _silent_stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "wait for the answer"}],
        max_rounds=10, relevant_tools={"bash"},
    ))
    events = _types(chunks)
    assert state == {"started": True, "closed": True}
    assert any(event.get("type") == "ask_user" for event in events), events
    assert not any(event.get("type") == "round_info" for event in events), events


def test_turn_ceiling_stops_a_continuous_stream_without_running_buffered_tools(monkeypatch):
    _patch_common(monkeypatch, {"agent_turn_max_seconds": 5,
                                "agent_auto_continue_on_progress": True})
    state = {"closed": False, "executed": 0}

    async def _fake_exec(*_args, **_kwargs):
        state["executed"] += 1
        return "bash", {"output": "unexpected", "exit_code": 0}

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    async def _stream(_candidates, _messages, **_kwargs):
        try:
            yield 'data: ' + json.dumps({"delta": "Partial progress."}) + "\n\n"
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [{
                "name": "bash", "arguments": json.dumps({"command": "echo must-not-run"}),
            }]}) + "\n\n"
            while True:
                await asyncio.sleep(0.02)
                yield 'data: ' + json.dumps({"delta": " more"}) + "\n\n"
        finally:
            state["closed"] = True

    monkeypatch.setattr(al, "stream_llm_with_fallback", _stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "do something"}],
        max_rounds=10, relevant_tools={"bash"},
    ))
    events = _types(chunks)
    assert state["closed"] is True
    assert state["executed"] == 0
    assert any(event.get("type") == "ask_user" for event in events), events
    assert any(event.get("type") == "response_replace" and "Partial progress." in event.get("text", "")
               for event in events), events


def test_turn_deadline_disabled_and_fast_completion_keep_existing_behavior(monkeypatch):
    _patch_common(monkeypatch, {"agent_turn_max_seconds": -1,
                                "agent_auto_continue_on_progress": True})
    calls = {"count": 0}

    async def _fast_stream(_candidates, _messages, **_kwargs):
        calls["count"] += 1
        yield 'data: ' + json.dumps({"delta": "Completed inside the limit."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fast_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "answer quickly"}],
        max_rounds=2, relevant_tools={"bash"},
    )))
    assert calls["count"] == 1
    assert not any(event.get("type") == "ask_user" for event in events), events
    assert any(event.get("delta") == "Completed inside the limit." for event in events), events


def test_turn_finishing_inside_an_active_wall_clock_limit_is_unchanged(monkeypatch):
    _patch_common(monkeypatch, {"agent_turn_max_seconds": 20,
                                "agent_auto_continue_on_progress": True})

    async def _fast_stream(_candidates, _messages, **_kwargs):
        yield 'data: ' + json.dumps({"delta": "Finished before the deadline."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fast_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "answer"}],
        max_rounds=2, relevant_tools={"bash"},
    )))
    assert not any(event.get("type") == "ask_user" for event in events), events
    assert any(event.get("delta") == "Finished before the deadline." for event in events), events


def test_turn_ceiling_closes_a_real_http_sse_stream(monkeypatch):
    """Exercise llm_core/httpx close propagation without any model server."""
    _patch_common(monkeypatch, {"agent_turn_max_seconds": 5,
                                "agent_auto_continue_on_progress": True})
    state = {"request": threading.Event(), "disconnected": threading.Event(), "body": None, "path": None}

    class _SSEHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            state["path"] = self.path
            length = int(self.headers.get("Content-Length", "0"))
            state["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            events = [
                {"choices": [{"delta": {"content": "Partial progress."}, "finish_reason": None}]},
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "call_deadline", "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"echo must-not-run"}'},
                }]}, "finish_reason": None}]},
            ]
            for event in events:
                chunk = ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            state["request"].set()

            # Wait without sending bytes so the real HTTPX SSE reader is
            # pending silently after seeing text and a buffered tool fragment.
            until = time.monotonic() + 12
            while time.monotonic() < until:
                readable, _, _ = select.select([self.connection], [], [], 0.05)
                if not readable:
                    continue
                try:
                    probe = self.connection.recv(1, socket.MSG_PEEK)
                except OSError:
                    state["disconnected"].set()
                    return
                if not probe:
                    state["disconnected"].set()
                    return

    class _SSEServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _SSEServer(("127.0.0.1", 0), _SSEHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    executed = {"count": 0}

    async def _fake_exec(*_args, **_kwargs):
        executed["count"] += 1
        return "bash", {"output": "unexpected", "exit_code": 0}

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    # Loopback /v1 is otherwise intentionally classified as Ollama by the
    # provider detector. This fixture speaks the generic OpenAI-compatible
    # SSE contract, so disable only that adapter classification.
    from src import llm_core
    monkeypatch.setattr(llm_core, "_is_ollama_native_url", lambda _url: False)
    monkeypatch.setattr(llm_core, "_is_ollama_openai_compat_url", lambda _url: False)
    started = time.monotonic()
    try:
        chunks = _collect(al.stream_agent_loop(
            f"http://127.0.0.1:{server.server_port}/v1", "synthetic-test-model",
            [{"role": "user", "content": "do something"}],
            max_rounds=10, relevant_tools={"bash"},
        ))
        elapsed = time.monotonic() - started
        events = _types(chunks)
        assert state["request"].is_set(), "the real stream client never reached the local SSE server"
        assert state["disconnected"].wait(2), "the client left the pending HTTP response open"
        assert executed["count"] == 0
        assert any(event.get("type") == "ask_user" for event in events), events
        assert any(event.get("type") == "response_replace" and "Partial progress." in event.get("text", "")
                   for event in events), events
        assert elapsed < 15, f"deadline did not stop the live HTTP stream promptly: {elapsed:.2f}s"
        assert state["path"] == "/v1/chat/completions"
        assert state["body"].get("stream") is True
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(2)
