"""Unit tests for src/retry_policy.py (CALL-06, spec §34.5).

Pure and network-free throughout: `classify_http`/`delay`/`RetryBudget` take
already-observed facts (a status, headers, a caught exception, an explicit
clock reading) and return data, so nothing here touches a socket or sleeps
for real.
"""
import random

import httpx
import pytest

from src.retry_policy import (
    RetryBudget,
    RetryClass,
    classify_http,
    delay,
    error_class_for,
    parse_retry_after,
)


# ── classify_http: status-driven ──

class TestClassifyHttpStatus:
    @pytest.mark.parametrize("status", [429, 503])
    def test_429_503_are_retry_now(self, status):
        assert classify_http(status=status) is RetryClass.RETRY_NOW

    @pytest.mark.parametrize("status", [502, 504])
    def test_502_504_are_retry_now(self, status):
        assert classify_http(status=status) is RetryClass.RETRY_NOW

    @pytest.mark.parametrize("status", [500, 501, 505, 599])
    def test_other_5xx_without_retry_after_is_retry_backoff(self, status):
        assert classify_http(status=status) is RetryClass.RETRY_BACKOFF

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_4xx_client_errors_never_retry(self, status):
        assert classify_http(status=status) is RetryClass.NO_RETRY

    def test_status_wins_over_exc_when_both_given(self):
        # A status code is a stronger fact than an incidental exception
        # raised while parsing that same response.
        exc = httpx.ReadTimeout("irrelevant")
        assert classify_http(status=400, exc=exc) is RetryClass.NO_RETRY


# ── classify_http: exception-driven ──

class TestClassifyHttpException:
    def test_connect_timeout_is_retry_backoff(self):
        assert classify_http(exc=httpx.ConnectTimeout("x")) is RetryClass.RETRY_BACKOFF

    def test_connect_error_is_retry_backoff(self):
        assert classify_http(exc=httpx.ConnectError("x")) is RetryClass.RETRY_BACKOFF

    def test_pool_timeout_is_retry_backoff(self):
        assert classify_http(exc=httpx.PoolTimeout("x")) is RetryClass.RETRY_BACKOFF

    def test_write_timeout_is_retry_backoff(self):
        assert classify_http(exc=httpx.WriteTimeout("x")) is RetryClass.RETRY_BACKOFF

    def test_read_timeout_after_body_sent_is_outcome_unknown(self):
        # httpx only raises ReadTimeout once the request has been written and
        # the client is waiting on a response — the defining "sent, then
        # lost" case from the lote's spec.
        assert classify_http(exc=httpx.ReadTimeout("x")) is RetryClass.OUTCOME_UNKNOWN

    def test_connection_reset_mid_response_is_outcome_unknown(self):
        assert classify_http(exc=httpx.ReadError("x")) is RetryClass.OUTCOME_UNKNOWN

    def test_remote_protocol_error_stream_cut_is_outcome_unknown(self):
        assert classify_http(exc=httpx.RemoteProtocolError("x")) is RetryClass.OUTCOME_UNKNOWN

    def test_unrecognised_transport_exception_defaults_to_retry_backoff(self):
        class SomeOtherTransportGlitch(Exception):
            pass

        assert classify_http(exc=SomeOtherTransportGlitch("x")) is RetryClass.RETRY_BACKOFF

    def test_no_status_and_no_exc_is_no_retry(self):
        assert classify_http() is RetryClass.NO_RETRY


# ── parse_retry_after ──

class TestParseRetryAfter:
    def test_absent_header_is_none(self):
        assert parse_retry_after({}) is None
        assert parse_retry_after(None) is None

    def test_seconds_form(self):
        assert parse_retry_after({"Retry-After": "12"}) == 12.0

    def test_seconds_form_is_case_insensitive_header_name(self):
        assert parse_retry_after({"retry-after": "7"}) == 7.0

    def test_http_date_form(self):
        headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:13 GMT"}
        # now = exactly 30s before the target date
        import calendar
        target = calendar.timegm((2015, 10, 21, 7, 28, 13, 0, 0, 0))
        result = parse_retry_after(headers, now=target - 30)
        assert result == pytest.approx(30.0, abs=0.5)

    def test_http_date_in_the_past_clamps_to_zero(self):
        headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:13 GMT"}
        import calendar
        target = calendar.timegm((2015, 10, 21, 7, 28, 13, 0, 0, 0))
        result = parse_retry_after(headers, now=target + 3600)
        assert result == 0.0

    def test_negative_seconds_is_treated_as_absent(self):
        assert parse_retry_after({"Retry-After": "-5"}) is None

    def test_garbage_value_is_treated_as_absent(self):
        assert parse_retry_after({"Retry-After": "not-a-date-or-number"}) is None


# ── delay ──

class TestDelay:
    def test_jitter_stays_within_0_to_base_times_2_pow_n(self):
        base = 0.5
        rng = random.Random(0)
        for attempt in range(1, 8):
            ceiling = min(60.0, base * (2 ** (attempt - 1)))
            for _ in range(50):
                d = delay(attempt, base=base, cap=60.0, rand=rng)
                assert 0.0 <= d <= ceiling

    def test_first_attempt_ceiling_is_base(self):
        rng = random.Random(1)
        for _ in range(50):
            assert 0.0 <= delay(1, base=0.5, rand=rng) <= 0.5

    def test_cap_bounds_large_attempts(self):
        rng = random.Random(2)
        for _ in range(50):
            assert delay(20, base=0.5, cap=3.0, rand=rng) <= 3.0

    def test_retry_after_is_honoured_verbatim_not_jittered(self):
        assert delay(1, retry_after=4.0, cap=60.0) == 4.0

    def test_retry_after_is_still_capped(self):
        assert delay(1, retry_after=999.0, cap=10.0) == 10.0

    def test_negative_retry_after_clamped_to_zero(self):
        assert delay(1, retry_after=-5.0) == 0.0


# ── RetryBudget ──

class TestRetryBudget:
    def test_not_exhausted_before_start(self):
        budget = RetryBudget(10.0)
        assert budget.exhausted() is False

    def test_exhausted_after_total_seconds_elapsed(self):
        budget = RetryBudget(10.0)
        budget.start(now=1000.0)
        assert budget.exhausted(now=1005.0) is False
        assert budget.exhausted(now=1010.0) is True
        assert budget.exhausted(now=1011.0) is True

    def test_remaining_counts_down_and_floors_at_zero(self):
        budget = RetryBudget(10.0)
        budget.start(now=1000.0)
        assert budget.remaining(now=1004.0) == pytest.approx(6.0)
        assert budget.remaining(now=1020.0) == 0.0


# ── error_class_for ──

class TestErrorClassFor:
    def test_429_is_resource_rate_limited(self):
        assert error_class_for(status=429) == "resource.rate_limited"

    def test_5xx_is_transport_llm_service_error(self):
        assert error_class_for(status=503) == "transport.llm_service_error"

    def test_404_is_resource_not_found(self):
        assert error_class_for(status=404) == "resource.not_found"

    def test_read_timeout_is_timeout_deadline_exceeded(self):
        assert error_class_for(exc=httpx.ReadTimeout("x")) == "timeout.deadline_exceeded"

    def test_connect_timeout_is_timeout_deadline_exceeded(self):
        assert error_class_for(exc=httpx.ConnectTimeout("x")) == "timeout.deadline_exceeded"

    def test_read_error_is_transport_connection_reset(self):
        assert error_class_for(exc=httpx.ReadError("x")) == "transport.connection_reset"

    def test_connect_error_is_transport_network_unreachable(self):
        assert error_class_for(exc=httpx.ConnectError("x")) == "transport.network_unreachable"

    def test_dotted_codes_use_known_obs03_categories(self):
        from src.contracts.errors import ERROR_CATEGORIES

        for status in (400, 401, 403, 404, 413, 422, 429, 500, 503):
            code = error_class_for(status=status)
            assert code.split(".", 1)[0] in ERROR_CATEGORIES
