# src/retry_policy.py
"""Retry classification and backoff for provider HTTP calls (CALL-06, spec §34.5).

Before this module `src/llm_core.py`'s provider-call retry loop treated every
429/502/503/504 the same way: sleep `LLMConfig.RETRY_DELAY` (a flat 0.5s, no
jitter) and try again, ignoring any `Retry-After` the provider sent and never
asking whether the *previous* attempt might already have reached the
provider. Both are real failure modes the spec names explicitly: a stampede
of clients retrying in lockstep, and — worse — resending a request whose
first copy the provider may already have executed (`outcome_unknown`,
QA-11). This module answers "what kind of failure was this" as a pure
function of already-observed facts (a status code, response headers, or a
caught transport exception), so the decision is unit-testable without a
socket and reusable by any future call site with the same shape of problem
(the spec calls out that a *tool* with remote effects — email, a post — must
reuse this classification and refuse to auto-retry `outcome_unknown`, even
though a bare LLM completion may, per `llm_core`'s own wiring).

Four closed classes, matching §34.5's `outcome_unknown` naming exactly:

  retry_now       — the provider told us this is transient (429/503,
                    honouring `Retry-After` when sent) or a gateway hiccup
                    (502/504). Safe and, per the provider, expected to clear.
  retry_backoff   — no explicit signal, but the failure is one where nothing
                    of the request could plausibly have landed yet: a
                    connect-phase timeout/error, or another 5xx with no
                    `Retry-After`. Safe to retry, paced with full jitter
                    since there is no better signal for the right delay.
  no_retry        — a 4xx: the request itself was rejected. Resending it
                    unchanged repeats the same mistake.
  outcome_unknown — the request body was already sent, or the provider had
                    already started answering, when the connection died: a
                    read timeout after the body was written, a reset mid-
                    response, or a stream cut after some bytes arrived.
                    Whether the provider executed the call is genuinely
                    unknown. Whether THAT is safe to retry depends on the
                    call's own idempotency, which this module cannot know —
                    it only hands the caller the classification (§34.5:
                    "consultar estado/deduplicación... o pedir reconciliación
                    humana", never a blind resend for an action with effects).

Nothing here imports httpx (or any transport library): exceptions are
matched by class name via the MRO, so a real `httpx.ReadTimeout`, a test
double built to look like one, or some other library's equivalently-named
exception all classify the same way, and this module stays importable
without pulling in the HTTP stack.
"""

from __future__ import annotations

import enum
import random
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping, Optional


class RetryClass(enum.Enum):
    """The four closed outcomes of `classify_http`. See module docstring."""

    RETRY_NOW = "retry_now"
    RETRY_BACKOFF = "retry_backoff"
    NO_RETRY = "no_retry"
    OUTCOME_UNKNOWN = "outcome_unknown"

    def __str__(self) -> str:  # so f"{classification}" logs the plain name
        return self.value


#: Statuses the provider itself flagged as "try again": 429/503 (honouring
#: Retry-After when sent) plus the two gateway statuses that are almost
#: always a transient hop failure rather than the model objecting to the
#: request itself.
_RETRY_NOW_STATUSES = frozenset({429, 502, 503, 504})

#: Transport exceptions from the connect/write phase: nothing has
#: necessarily reached the provider yet, so retrying is safe, but there is no
#: Retry-After to honour, hence backoff rather than retry_now.
_BACKOFF_EXC_NAMES = frozenset({
    "ConnectTimeout", "ConnectError", "PoolTimeout",
    "WriteTimeout", "WriteError", "CloseError", "LocalProtocolError",
})

#: Exceptions that only happen once bytes were already exchanged with the
#: provider (the body was written and we were waiting on / receiving the
#: response): the call's effect, if any, cannot be ruled out.
_OUTCOME_UNKNOWN_EXC_NAMES = frozenset({
    "ReadTimeout", "ReadError", "RemoteProtocolError",
})


def _exc_class_names(exc: BaseException) -> frozenset:
    return frozenset(t.__name__ for t in type(exc).__mro__)


def _find_header(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    if not headers:
        return None
    lname = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lname:
            return value
    return None


def classify_http(status: Optional[int] = None,
                   headers: Optional[Mapping[str, str]] = None,
                   exc: Optional[BaseException] = None) -> RetryClass:
    """Classify one failed attempt into a closed `RetryClass`.

    A real call has either a received HTTP response (`status`, optionally
    `headers`) or a transport exception that means no response arrived at
    all (`exc`) — never both. Both parameters are accepted so the caller can
    pass whichever it has without an `if` at the call site; `status` wins
    when both are given (a status code is a stronger fact than an exception
    that fired while parsing the same response).
    """
    if status is not None:
        if status in _RETRY_NOW_STATUSES:
            return RetryClass.RETRY_NOW
        if 500 <= status < 600:
            # An OBS-03-visible 5xx this taxonomy did not single out above
            # (500, 501, 505...): no provider signal to honour, but the
            # request is not a client mistake either — back off and retry.
            return RetryClass.RETRY_BACKOFF
        # Everything else observed as a status (4xx, or a non-error status a
        # caller mistakenly passed in) is not something blind resending fixes.
        return RetryClass.NO_RETRY
    if exc is not None:
        names = _exc_class_names(exc)
        if names & _OUTCOME_UNKNOWN_EXC_NAMES:
            return RetryClass.OUTCOME_UNKNOWN
        if names & _BACKOFF_EXC_NAMES:
            return RetryClass.RETRY_BACKOFF
        # An unrecognised transport exception: same reasoning as an
        # unenumerated 5xx above — nothing suggests the request landed, so
        # back off and retry rather than give up outright.
        return RetryClass.RETRY_BACKOFF
    return RetryClass.NO_RETRY


def parse_retry_after(headers: Optional[Mapping[str, str]], *,
                       now: Optional[float] = None) -> Optional[float]:
    """`Retry-After` as seconds from `now`, or `None` if absent/unparseable.

    RFC 9110 §10.2.3 allows either a delay-in-seconds or an HTTP-date; both
    forms are seen in the wild (most LLM providers send seconds; some
    fronting gateways send a date), so both are honoured. A negative or
    unparseable value is treated as absent rather than raising — a
    malformed header from the provider should fall back to our own backoff,
    not blow up the retry loop.
    """
    raw = _find_header(headers, "retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if seconds >= 0 else None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = datetime.fromtimestamp(now if now is not None else _time.time(), tz=timezone.utc)
    return max((when - reference).total_seconds(), 0.0)


def delay(attempt: int, *, retry_after: Optional[float] = None,
          base: float = 0.5, cap: float = 60.0,
          rand: Optional[random.Random] = None) -> float:
    """Seconds to sleep before the next attempt.

    Full jitter (AWS's "Exponential Backoff And Jitter"): a uniform draw
    from `[0, min(cap, base * 2**(attempt-1))]`. `attempt` counts the
    attempt that just failed (1 for the first failure), so the first
    backoff window is `[0, base]`, the second `[0, base*2]`, and so on up to
    `cap`.

    `retry_after`, when the provider sent one, wins outright and is NOT
    jittered — jittering a value the provider explicitly asked for would
    just make us wait less than requested half the time, which is the exact
    stampede behaviour Retry-After exists to prevent. It is still clamped to
    `[0, cap]` so a misconfigured or hostile Retry-After cannot stall a
    caller indefinitely; pass a larger `cap` for callers that want to honour
    longer waits verbatim.
    """
    if retry_after is not None:
        return max(0.0, min(float(retry_after), cap))
    attempt = max(1, int(attempt))
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    rng = rand or random
    return rng.uniform(0.0, ceiling)


@dataclass
class RetryBudget:
    """Wall-clock ceiling on a whole retry sequence, independent of attempt count.

    `max_retries` alone can let an unlucky string of long `Retry-After`
    waits burn far more wall time than a caller intended (three attempts at
    a 30s Retry-After each is 90s of waiting); a budget stops the sequence
    on whichever limit — attempts or time — is hit first. Pure: `start()`
    takes an explicit clock reading so tests do not need to sleep for real.
    """

    total_seconds: float
    _deadline: Optional[float] = field(default=None, repr=False, compare=False)

    def start(self, now: Optional[float] = None) -> None:
        self._deadline = (now if now is not None else _time.time()) + self.total_seconds

    def exhausted(self, now: Optional[float] = None) -> bool:
        if self._deadline is None:
            return False
        return (now if now is not None else _time.time()) >= self._deadline

    def remaining(self, now: Optional[float] = None) -> float:
        if self._deadline is None:
            return self.total_seconds
        return max(0.0, self._deadline - (now if now is not None else _time.time()))


def error_class_for(*, status: Optional[int] = None,
                     exc: Optional[BaseException] = None) -> str:
    """The OBS-03 dotted code (`category.subcode`, `src/contracts/errors.py`)
    this failure counts as.

    Kept next to the retry classification — not the same axis: a 502 is
    `transport.llm_service_error` whether or not the caller chooses to
    retry it — so a caller exposing `error_class` on its final result (as
    §34.5 requires) has one place both come from instead of re-deriving the
    mapping at each call site.
    """
    if status is not None:
        if status == 429:
            return "resource.rate_limited"
        if 500 <= status < 600:
            return "transport.llm_service_error"
        if status in (401, 403):
            return "permission.denied"
        if status == 404:
            return "resource.not_found"
        if status == 413:
            return "schema.invalid_upload"
        return "schema.contract_violation"
    if exc is not None:
        names = _exc_class_names(exc)
        if "ReadTimeout" in names:
            return "timeout.deadline_exceeded"
        if names & _OUTCOME_UNKNOWN_EXC_NAMES:
            return "transport.connection_reset"
        if names & {"ConnectTimeout", "PoolTimeout", "WriteTimeout"}:
            return "timeout.deadline_exceeded"
        return "transport.network_unreachable"
    return "unknown.unmapped_exception"
