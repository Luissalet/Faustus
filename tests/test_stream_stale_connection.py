"""A pooled connection the server already closed is retried once, even by a caller that fails fast."""
import httpx

from src import llm_core
from src.retry_policy import RetryBudget


def decide(exc, *, attempt=1, delta_emitted=False, fail_fast=True):
    return llm_core._stream_retry_decision(exc=exc, attempt=attempt, max_retries=3, budget=RetryBudget(30.0),
                                           delta_emitted=delta_emitted, fail_fast=fail_fast)


def test_a_stale_keep_alive_connection_gets_one_immediate_retry():
    stale = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    should_retry, wait, _, retryable = decide(stale)
    assert should_retry is True and wait == 0.0 and retryable is True
    # Only once, and never after text reached the client.
    assert decide(stale, attempt=2)[0] is False
    assert decide(stale, delta_emitted=True)[0] is False


def test_other_protocol_failures_keep_failing_fast():
    cut = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    assert decide(cut)[0] is False
    assert decide(httpx.ConnectError("refused"))[0] is False
