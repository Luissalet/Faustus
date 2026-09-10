"""Integration tests for the provider-call retry loop in src/llm_core.py
(CALL-06, spec §34.5): llm_call_async wired to src.retry_policy.

The pure classification/backoff math is tested directly in
test_retry_policy.py; these tests exercise the call site — replacing
`httpx_post_kimi_aware_async` with a scripted sequence of fake
responses/exceptions, the same mocking pattern test_llm_core_dns_pin.py and
test_llm_core_fallback.py already use for this module (`conftest.py` stubs
the heavy deps so importing the real `src.llm_core` is side-effect free; no
test here touches the network). `asyncio.sleep` is patched to a fast no-op
recorder so a multi-attempt sequence does not slow the suite down while
still letting tests assert what delay each attempt actually asked for.
"""
import email.utils
import time

import httpx
import pytest

import src.llm_core as llm_core
from src.retry_policy import RetryClass


class _Response:
    """Minimal stand-in for an httpx.Response, matching what the loop reads."""

    def __init__(self, status_code, headers=None, json_body=None):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.headers = headers or {}
        self.text = "" if self.is_success else '{"error": "upstream failed"}'

    def json(self):
        return {"model": "test-model", "choices": [{"message": {"content": "ok"}}]}


def _wire_sequence(monkeypatch, events):
    """Replace the provider POST with a scripted sequence.

    Each entry in `events` is either an `_Response` to return or a
    `BaseException` instance to raise, consumed in call order. Also stubs the
    dead-host bookkeeping and activity logging exactly like
    test_llm_core_dns_pin.py does, so a test's failures never leak into the
    module-level `_dead_hosts` state other tests read.
    """
    calls = []

    async def _fake_post(client, url, headers, **kwargs):
        event = events[len(calls)]
        calls.append(event)
        if isinstance(event, BaseException):
            raise event
        return event

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", _fake_post)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    return calls


def _fast_sleep(monkeypatch):
    """Record every asyncio.sleep the loop asks for without actually waiting."""
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_core.asyncio, "sleep", _sleep)
    return sleeps


def _call(monkeypatch, url, *, max_retries=3, on_outcome_unknown=None):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    llm_core._response_cache.clear()
    import asyncio as real_asyncio
    return real_asyncio.run(llm_core.llm_call_async(
        url, "test-model",
        [{"role": "user", "content": f"unique retry-policy request for {url}"}],
        max_retries=max_retries,
        on_outcome_unknown=on_outcome_unknown,
    ))


# ── Retry-After honoured (seconds and HTTP-date) ──

class TestRetryAfterHonoured:
    def test_429_with_retry_after_seconds_then_succeeds(self, monkeypatch):
        sleeps = _fast_sleep(monkeypatch)
        _wire_sequence(monkeypatch, [
            _Response(429, headers={"Retry-After": "2"}),
            _Response(200),
        ])
        result = _call(monkeypatch, "https://api.example-ra-seconds.test/v1/chat/completions",
                        max_retries=2)
        assert result == "ok"
        assert sleeps == [2.0]  # honoured verbatim, not jittered

    def test_503_with_retry_after_http_date_then_succeeds(self, monkeypatch):
        sleeps = _fast_sleep(monkeypatch)
        target = time.time() + 5
        header_date = email.utils.formatdate(target, usegmt=True)
        _wire_sequence(monkeypatch, [
            _Response(503, headers={"Retry-After": header_date}),
            _Response(200),
        ])
        result = _call(monkeypatch, "https://api.example-ra-date.test/v1/chat/completions",
                        max_retries=2)
        assert result == "ok"
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(5.0, abs=2.0)  # generous: test wall-clock slack


# ── 4xx never retries ──

class TestNoRetryOnClientError:
    def test_400_raises_on_first_attempt_no_retry(self, monkeypatch):
        sleeps = _fast_sleep(monkeypatch)
        calls = _wire_sequence(monkeypatch, [_Response(400)])
        with pytest.raises(llm_core.HTTPException) as exc_info:
            _call(monkeypatch, "https://api.example-400.test/v1/chat/completions",
                  max_retries=3)
        assert len(calls) == 1  # never retried
        assert sleeps == []
        exc = exc_info.value
        assert exc.status_code == 400
        assert exc.attempts == 1
        assert exc.retryable is False
        assert exc.error_class.split(".")[0] in ("schema", "permission", "resource")


# ── 429 retries and stops when attempts are exhausted ──

class TestRetryNowExhaustsAttempts:
    def test_429_retries_until_max_retries_then_raises(self, monkeypatch):
        sleeps = _fast_sleep(monkeypatch)
        calls = _wire_sequence(monkeypatch, [
            _Response(429), _Response(429), _Response(429),
        ])
        with pytest.raises(llm_core.HTTPException) as exc_info:
            _call(monkeypatch, "https://api.example-429-exhaust.test/v1/chat/completions",
                  max_retries=3)
        assert len(calls) == 3
        assert len(sleeps) == 2  # slept before attempts 2 and 3, not after the final failure
        exc = exc_info.value
        assert exc.status_code == 429
        assert exc.attempts == 3
        assert exc.retryable is True  # retry_now is a retryable class even once attempts run out
        assert exc.error_class == "resource.rate_limited"

    def test_previously_retried_gateway_codes_still_retry(self, monkeypatch):
        # Backward compatibility: 502/503/504 (already retried pre-CALL-06)
        # keep retrying exactly as before, now via the same classifier.
        sleeps = _fast_sleep(monkeypatch)
        _wire_sequence(monkeypatch, [
            _Response(502), _Response(503), _Response(504), _Response(200),
        ])
        result = _call(monkeypatch, "https://api.example-gateway-codes.test/v1/chat/completions",
                        max_retries=4)
        assert result == "ok"
        assert len(sleeps) == 3

    def test_5xx_without_retry_after_backs_off_within_jitter_window(self, monkeypatch):
        # New capability (CALL-06): a 500 (not one of the four legacy codes)
        # now also retries, via retry_backoff, jittered within [0, base].
        sleeps = _fast_sleep(monkeypatch)
        _wire_sequence(monkeypatch, [_Response(500), _Response(200)])
        result = _call(monkeypatch, "https://api.example-500.test/v1/chat/completions",
                        max_retries=2)
        assert result == "ok"
        assert len(sleeps) == 1
        assert 0.0 <= sleeps[0] <= 0.5  # attempt=1 ceiling is `base` (0.5s default)


# ── read timeout after the body was sent → outcome_unknown ──

class TestOutcomeUnknownReadTimeout:
    def test_read_timeout_calls_hook_then_retries_and_succeeds(self, monkeypatch):
        # A plain completion's only effect is provider-side tokens spent, so
        # §34.5 allows retrying outcome_unknown here — but the hook must still
        # fire so the caller can log/account for the ambiguity.
        sleeps = _fast_sleep(monkeypatch)
        hook_calls = []
        _wire_sequence(monkeypatch, [
            httpx.ReadTimeout("upstream stopped answering"),
            _Response(200),
        ])
        result = _call(
            monkeypatch, "https://api.example-read-timeout-retry.test/v1/chat/completions",
            max_retries=2, on_outcome_unknown=lambda attempt, elapsed: hook_calls.append(attempt),
        )
        assert result == "ok"
        assert hook_calls == [1]
        assert len(sleeps) == 1

    def test_read_timeout_exhausted_raises_with_outcome_unknown_error_class(self, monkeypatch):
        sleeps = _fast_sleep(monkeypatch)
        hook_calls = []
        _wire_sequence(monkeypatch, [
            httpx.ReadTimeout("a"), httpx.ReadTimeout("b"),
        ])
        with pytest.raises(llm_core.HTTPException) as exc_info:
            _call(
                monkeypatch, "https://api.example-read-timeout-exhaust.test/v1/chat/completions",
                max_retries=2, on_outcome_unknown=lambda attempt, elapsed: hook_calls.append(attempt),
            )
        assert hook_calls == [1, 2]
        exc = exc_info.value
        assert exc.status_code == 504
        assert exc.attempts == 2
        assert "outcome" in exc.detail.lower()  # message distinguishes this from a plain timeout
        assert exc.error_class == "timeout.deadline_exceeded"
        # Callers must be told this is NOT retryable automatically for
        # anything with a remote effect (§34.5 / QA-11).
        assert exc.retryable is False

    def test_hook_is_never_called_for_a_plain_client_error(self, monkeypatch):
        hook_calls = []
        _fast_sleep(monkeypatch)
        _wire_sequence(monkeypatch, [_Response(404)])
        with pytest.raises(llm_core.HTTPException):
            _call(
                monkeypatch, "https://api.example-404-no-hook.test/v1/chat/completions",
                max_retries=2, on_outcome_unknown=lambda attempt, elapsed: hook_calls.append(attempt),
            )
        assert hook_calls == []


# ── classify_http itself is what the loop's decisions trace back to ──

def test_loop_decision_matches_pure_classifier_for_429():
    from src.retry_policy import classify_http
    assert classify_http(status=429) is RetryClass.RETRY_NOW
