"""Integration tests for the streaming retry loops in src/llm_core.py
(CALL-06 completing for streaming, spec §34.5; MOD-03 partial: refusal
typing).

Lote 7 (tests/test_llm_core_retries.py) wired `src.retry_policy` into the
non-streaming `llm_call_async` loop; this file exercises the SAME policy
now applied to the four streaming provider branches inside
`_stream_llm_inner` (chatgpt-subscription, ollama, anthropic, the generic
OpenAI-compatible path) — classify_http, Retry-After, jitter, RetryBudget,
and the streaming-specific rule: a cut AFTER at least one delta was already
forwarded to the client is `outcome_unknown` and is never retried (that
would resend the prompt and duplicate the visible text); it is surfaced as
a typed error with `partial: true` instead.

`stream_llm`/`_stream_llm_inner` retry by tail-recursing into a fresh
attempt rather than looping in place (see `_stream_llm_inner`'s docstring),
so each retry is a fresh call to `_get_http_client().stream(...)` — the
fakes below hand out one canned attempt per call, in order, and raise
`IndexError` if the loop asks for one more attempt than a test provided
(the cleanest possible signal that a "never retries after partial output"
assertion was violated).
"""
import json
import asyncio

import httpx
import pytest

from src import llm_core


class _Resp:
    """Minimal stand-in for an httpx streaming Response."""

    def __init__(self, lines=None, status_code=200, headers=None, raise_after=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._lines = lines or []
        self._raise_after = raise_after

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._raise_after is not None:
            raise self._raise_after

    async def aread(self):
        return b"upstream error body"


class _StreamCtx:
    """One attempt's `client.stream(...)` context manager: either raises on
    connect (`raise_on_enter`, simulating a pre-content transport failure)
    or hands back a `_Resp`."""

    def __init__(self, resp=None, raise_on_enter=None):
        self._resp = resp
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._resp

    async def __aexit__(self, *args):
        return False


class _SeqClient:
    """Replays one `_StreamCtx` per `.stream()` call, in order. A call past
    the end of the list raises IndexError — any test relying on "this must
    not retry" simply omits a second entry, so a regression that retries
    anyway fails loudly instead of silently passing."""

    def __init__(self, contexts):
        self._contexts = list(contexts)
        self.calls = 0

    def stream(self, method, url, **kwargs):
        ctx = self._contexts[self.calls]
        self.calls += 1
        return ctx


def _fast_sleep(monkeypatch):
    """Record every asyncio.sleep the loop asks for without actually waiting."""
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_core.asyncio, "sleep", _sleep)
    return sleeps


def _drive(monkeypatch, contexts, *, url="https://api.example-stream-retry.test/v1/chat/completions",
           model="test-model", **kwargs):
    """Wire a `_SeqClient` in place of the real transport and run `stream_llm`
    to completion, returning `(client, chunks)`."""
    client = _SeqClient(contexts)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    # Neutralise the dead-host cooldown counters: this file's own retry vs.
    # not-retry assertions must not depend on cross-test cooldown state, the
    # same isolation test_llm_core_retries.py uses for the non-streaming loop.
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm(
                url, model, [{"role": "user", "content": "hi"}], **kwargs,
            )
        ]

    return client, asyncio.run(run())


def _events(chunks):
    """Parse every `data: {...}` JSON payload out of the raw SSE chunks
    (an `event: error\\ndata: {...}` chunk carries its payload the same way
    a plain `data: {...}` one does, so both parse uniformly)."""
    out = []
    for chunk in chunks:
        for raw_line in chunk.split("\n"):
            raw_line = raw_line.strip()
            if raw_line.startswith("data: ") and raw_line[6:] != "[DONE]":
                try:
                    out.append(json.loads(raw_line[6:]))
                except ValueError:
                    pass
    return out


# ── happy path is unchanged: exactly one physical attempt ──

def test_happy_path_makes_exactly_one_attempt(monkeypatch):
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"hi there"}}]}',
            "data: [DONE]",
        ])),
    ])
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "hi there"
    assert client.calls == 1


# ── Retry-After honoured on a non-2xx stream response ──

def test_status_429_honours_retry_after_then_succeeds(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(status_code=429, headers={"Retry-After": "2"})),
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ])),
    ], max_retries=2)
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "ok"
    assert client.calls == 2
    assert sleeps == [2.0]  # honoured verbatim, not jittered


def test_status_400_never_retries(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(status_code=400)),
    ], max_retries=3)
    assert client.calls == 1
    assert sleeps == []
    events = _events(chunks)
    assert len(events) == 1
    assert events[0]["status"] == 400
    assert events[0]["attempts"] == 1
    assert events[0]["retryable"] is False


def test_status_429_exhausts_max_retries_then_fails(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(status_code=429)),
        _StreamCtx(resp=_Resp(status_code=429)),
        _StreamCtx(resp=_Resp(status_code=429)),
    ], max_retries=3)
    assert client.calls == 3
    assert len(sleeps) == 2  # slept before attempts 2 and 3, not after the last failure
    events = _events(chunks)
    assert events[-1]["status"] == 429
    assert events[-1]["attempts"] == 3
    assert events[-1]["retryable"] is True


# ── connect-phase transport failures retry transparently (no content lost) ──

def test_connect_error_retries_then_succeeds(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(raise_on_enter=httpx.ConnectError("connection refused")),
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "data: [DONE]",
        ])),
    ], max_retries=2)
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "hi"
    assert client.calls == 2
    assert len(sleeps) == 1


def test_pool_timeout_retries_then_succeeds(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(raise_on_enter=httpx.PoolTimeout("pool exhausted")),
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "data: [DONE]",
        ])),
    ], max_retries=2)
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "hi"
    assert client.calls == 2
    assert len(sleeps) == 1


# ── read timeout before any content behaves like the non-streaming loop ──

def test_read_timeout_before_any_content_retries(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(lines=[], raise_after=httpx.ReadTimeout("no bytes arrived"))),
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ])),
    ], max_retries=2)
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "ok"
    assert client.calls == 2
    assert len(sleeps) == 1


# ── the streaming-specific rule: a cut AFTER a delta was forwarded is
#    outcome_unknown, is NEVER retried, and is reported as partial ──

def test_read_timeout_after_content_stops_with_partial_and_no_retry(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(
            lines=['data: {"choices":[{"delta":{"content":"Hello "}}]}'],
            raise_after=httpx.ReadTimeout("cut mid-answer"),
        )),
        # Deliberately no second attempt: a wrongful retry raises IndexError
        # here instead of silently duplicating "Hello ".
    ], max_retries=3)
    assert client.calls == 1
    assert sleeps == []
    events = _events(chunks)
    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["Hello "]  # the partial answer is kept, not discarded
    errors = [e for e in events if "error" in e]
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["retryable"] is False
    assert errors[0]["error_class"] == "timeout.deadline_exceeded"
    assert errors[0]["attempts"] == 1


def test_protocol_error_after_content_stops_with_partial_and_no_retry(monkeypatch):
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(
            lines=['data: {"choices":[{"delta":{"content":"partial answer"}}]}'],
            raise_after=httpx.RemoteProtocolError("peer reset the connection"),
        )),
    ], max_retries=3)
    assert client.calls == 1
    events = _events(chunks)
    assert [e["delta"] for e in events if "delta" in e] == ["partial answer"]
    errors = [e for e in events if "error" in e]
    assert len(errors) == 1
    assert errors[0]["partial"] is True


# ── availability_only_transport (used by a caller running its own fallback
#    chain, e.g. stream_llm_with_fallback) fails fast: one attempt, never
#    retried, matching llm_call_async's parameter of the same name ──

def test_availability_only_transport_fails_fast_on_write_timeout(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(raise_on_enter=httpx.WriteTimeout("write timed out")),
    ], max_retries=3, availability_only_transport=True)
    assert client.calls == 1
    assert sleeps == []
    events = _events(chunks)
    assert events[0]["retryable"] is False
    assert events[0].get("fallback_eligible") is False


def test_availability_only_transport_fails_fast_on_pool_timeout(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(raise_on_enter=httpx.PoolTimeout("pool exhausted")),
    ], max_retries=3, availability_only_transport=True)
    assert client.calls == 1
    assert sleeps == []
    events = _events(chunks)
    assert events[0]["retryable"] is False


# ── MOD-03: a provider refusal gets its own typed event, never disguised
#    as ordinary answer text ──

def test_openai_compatible_refusal_field_becomes_typed_event(monkeypatch):
    client, chunks = _drive(monkeypatch, [
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"refusal":"I can\'t help with that."}}]}',
            "data: [DONE]",
        ])),
    ])
    events = _events(chunks)
    assert not any("delta" in e for e in events)  # never leaks out as plain text
    refusals = [e for e in events if e.get("type") == "refusal"]
    assert len(refusals) == 1
    assert refusals[0]["text"] == "I can't help with that."


def test_anthropic_stop_reason_refusal_becomes_typed_event(monkeypatch):
    lines = [
        "data: " + json.dumps({
            "type": "message_start",
            "message": {"model": "claude-test", "usage": {"input_tokens": 5}},
        }),
        "data: " + json.dumps({
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"},
        }),
        "data: " + json.dumps({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "I can't help with that."},
        }),
        "data: " + json.dumps({
            "type": "message_delta", "delta": {"stop_reason": "refusal"},
            "usage": {"output_tokens": 8},
        }),
        "data: " + json.dumps({"type": "message_stop"}),
    ]
    client, chunks = _drive(
        monkeypatch, [_StreamCtx(resp=_Resp(lines=lines))],
        url="https://api.anthropic.com/v1/messages", model="claude-test",
    )
    events = _events(chunks)
    refusals = [e for e in events if e.get("type") == "refusal"]
    assert len(refusals) == 1
    assert refusals[0]["stop_reason"] == "refusal"


# ── classify_http itself is what these decisions trace back to ──

def test_loop_decision_matches_pure_classifier_for_429():
    from src.retry_policy import classify_http, RetryClass
    assert classify_http(status=429) is RetryClass.RETRY_NOW
