"""How old is too old, per field, with the clock passed in.

Section 6 of the plan in one module. Everything here is a pure function of
(observation time, TTL, now); there is no module-level clock and no
`datetime.now()` outside `_now()`, so a test can age a field by a week without
sleeping and two callers reading the same state at the same instant always get
the same answer.

The three decisions this file makes, and why each one is the way it is:

**A TTL is a property of the FIELD, not of the entity.** `service_state.health`
is worthless after a minute; `service_state.capabilities` is good for an hour.
Rating both by the entity's age would either refresh the capability list every
15 seconds or present a dead service as available. `TTL_SECONDS` is therefore
keyed by `"<schema>.<field>"`, with a per-schema default under the schema's own
name and a global floor beneath that.

**`aging` is a real rung, not a rounding error.** A field is `fresh` inside its
TTL, `aging` for the same length again, and `stale` after that. Two multiples
rather than one because section 6.3 has three risk levels and needs a middle
one: "good enough to tell you where things are, not good enough to delete
something with".

**`unknown` is the answer to an unreadable timestamp.** Not `stale`, which
would say "we looked and it was too long ago", and never `fresh`. This copies
`context_engine.store.age_seconds`, whose docstring paid for the lesson: a
corrupt field that reads as zero age becomes a permanent pass.

The numbers themselves come from section 6.1's table. Where a field has an
analogue in `src/context_engine/ranking.py::FRESHNESS_HALF_LIFE_DAYS`, the TTL
is chosen so the two agree about which sources go off quickly -- the Context
Engine already believes a `state` source is worthless in half a day, and a
second, contradictory opinion about that would be worse than either.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.state_mirror.contracts import (
    FRESHNESS_RATINGS,
    MINIMUM_FRESHNESS_LEVELS,
    MIXED_FRESHNESS,
    SCHEMA_FIELDS,
    FieldState,
    StateError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TTL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "AGING_MULTIPLE",
    "FRESH",
    "AGING",
    "STALE",
    "UNKNOWN",
    "ttl_for",
    "age_seconds",
    "rate",
    "rate_field",
    "worst",
    "combined",
    "acceptable",
    "meets",
    "refresh_action",
    "explain",
]

FRESH = "fresh"
AGING = "aging"
STALE = "stale"
UNKNOWN = "unknown"

#: How long past its TTL a field stays `aging` before it goes `stale`. One
#: extra TTL, so the three rungs are [0, ttl), [ttl, 2*ttl), [2*ttl, inf).
#: A single multiple keeps the arithmetic explainable in one sentence, which
#: matters because `explain()` has to say it to a user.
AGING_MULTIPLE = 2.0

#: The floor for anything with no policy at all. Deliberately short: an
#: unlisted field is one nobody thought about, and treating it as long-lived is
#: the failure this module exists to prevent.
DEFAULT_TTL_SECONDS = 60

#: Section 6.1, keyed `"<schema>.<field>"` for a field policy and `"<schema>"`
#: for the schema's default. Read by `ttl_for()`, which falls back in that
#: order and then to `DEFAULT_TTL_SECONDS`.
TTL_SECONDS: Dict[str, int] = {
    # -- the machine. Sampled constantly while work runs, and the numbers are
    # meaningless a minute later, so nothing here is generous.
    "device_state.v1": 5,
    "device_state.v1.disk_free_bytes": 60,          # moves slowly, costs a stat

    # -- models. `loaded` and `active_requests` change with every request;
    # `context_capacity` is a property of the model file and changes when the
    # model does.
    "model_state.v1": 15,
    "model_state.v1.loaded": 5,
    "model_state.v1.active_requests": 5,
    "model_state.v1.vram_bytes": 5,
    "model_state.v1.shared_memory_bytes": 5,
    "model_state.v1.tokens_per_second": 30,
    "model_state.v1.context_capacity": 3600,
    "model_state.v1.backend": 3600,
    "model_state.v1.availability": 60,

    # -- services. "available 45 seconds ago" is the honest ceiling for a
    # health probe; the capability list behind it is far more stable.
    "service_state.v1": 45,
    "service_state.v1.health": 30,
    "service_state.v1.latency_ms": 30,
    "service_state.v1.capabilities": 1800,

    # -- runs. Event-driven, so the TTL is a backstop for a lost event rather
    # than a polling interval: a run whose heartbeat stopped 20 seconds ago is
    # a run something should look at.
    "run_state.v1": 20,
    "run_state.v1.status": 15,
    "run_state.v1.progress": 10,
    "run_state.v1.worker_states": 15,
    "run_state.v1.last_heartbeat": 15,
    "run_state.v1.started_at": 86400,               # a fact about the past
    "run_state.v1.engine": 86400,
    "run_state.v1.label": 86400,
    "run_state.v1.proof_status": 300,

    # -- the workspace. Section 6.1 says "by event plus reconciliation": the
    # branch changes rarely and loudly, the working tree changes constantly and
    # silently.
    "project_state.v1": 120,
    "project_state.v1.current_branch": 300,
    "project_state.v1.head": 300,
    "project_state.v1.dirty": 60,
    "project_state.v1.changed_paths": 60,
    "project_state.v1.changed_count": 60,
    "project_state.v1.workspace_available": 300,
    "project_state.v1.active_objectives": 600,
    "project_state.v1.blocked_objectives": 600,
    "project_state.v1.last_verified_changeset": 3600,
    "project_state.v1.checkpoint_head": 300,

    # -- artifacts. A file on disk does not change on its own, so the policy is
    # "until a filesystem event or a check before use" (section 6.1). An hour
    # is the reconciliation interval, not a claim about the file.
    "artifact_state.v1": 3600,
    "artifact_state.v1.approval": 60,               # a person can decide any moment
    "artifact_state.v1.publication_state": 300,
    "artifact_state.v1.stale_derivatives": 600,

    # -- connections. Valid until expiry, an error, or a health check.
    "connection_state.v1": 900,
    "connection_state.v1.reachable": 120,
    "connection_state.v1.authenticated": 300,
    "connection_state.v1.configured": 3600,
    "connection_state.v1.expires_at": 3600,

    # -- objectives and approvals. Both change only when a person or an agent
    # does something, and both are read from a local file.
    "objective_state.v1": 600,
    "approval_state.v1": 60,
    "approval_state.v1.status": 30,                 # the whole point is waiting

    # -- sessions and council rooms. Event-driven; the TTL catches a dropped
    # stream rather than a slow one.
    "session_state.v1": 60,
    "council_state.v1": 30,
}


def _now(now: Any = None) -> datetime:
    """The clock, injectable. A naive datetime is read as UTC."""
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if isinstance(now, str) and now.strip():
        parsed = _parse(now)
        if parsed is not None:
            return parsed
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.fromtimestamp(float(now), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    """An ISO-8601 string as an aware datetime, or `None`.

    `None` is not an error and not zero: it is what an unreadable timestamp
    produces, and every caller here turns it into `unknown`.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ttl_for(schema: Any, field: Any = "") -> int:
    """Seconds this field stays `fresh`. Field policy, then schema, then floor.

    Never zero: a TTL of zero would make everything `stale` the instant it was
    written, which reads as a broken mirror rather than as a strict one.
    """
    schema_id = str(schema or "").strip()
    name = str(field or "").strip()
    if schema_id and name:
        specific = TTL_SECONDS.get(f"{schema_id}.{name}")
        if specific:
            return max(1, int(specific))
    if schema_id:
        general = TTL_SECONDS.get(schema_id)
        if general:
            return max(1, int(general))
    return DEFAULT_TTL_SECONDS


def age_seconds(observed_at: Any, *, now: Any = None) -> Optional[float]:
    """How long ago, or `None` when the timestamp cannot be read.

    `None` is not `0.0`. This is the same rule and the same reason as
    `context_engine.store.age_seconds`: a corrupt timestamp that reads as zero
    age is a permanent pass, and the one field nobody can date is exactly the
    one worth doubting.

    A timestamp in the FUTURE answers `0.0` rather than a negative number. A
    remote clock running fast is a known condition (section 8.2) and the honest
    reading of "observed in 30 seconds' time" is "just now", not "fresher than
    fresh".
    """
    seen = _parse(observed_at)
    if seen is None:
        return None
    return max(0.0, (_now(now) - seen).total_seconds())


def rate(observed_at: Any, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
         now: Any = None) -> str:
    """One of `fresh|aging|stale|unknown` for one timestamp.

    The whole of section 6.2 is these four lines. `aging` occupies the second
    TTL so that section 6.3's middle risk level has something to accept.
    """
    age = age_seconds(observed_at, now=now)
    if age is None:
        return UNKNOWN
    ttl = max(1, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    if age < ttl:
        return FRESH
    if age < ttl * AGING_MULTIPLE:
        return AGING
    return STALE


def rate_field(state: FieldState, *, schema: Any = "", field: Any = "",
               now: Any = None) -> str:
    """Re-rate a stored field against the clock now.

    The field's own `ttl_seconds` wins when it has one -- an adapter that knows
    its probe is good for exactly 15 seconds says so on the observation, and
    that is better information than a table. Otherwise the policy applies.
    """
    ttl = int(getattr(state, "ttl_seconds", 0) or 0) or ttl_for(schema, field)
    return rate(getattr(state, "observed_at", ""), ttl_seconds=ttl, now=now)


def worst(ratings: Sequence[str]) -> str:
    """The least trustworthy rating in a set, for a caller that needs one word.

    Ordered by `FRESHNESS_RATINGS`, so `unknown` beats `stale` beats `aging`.
    An empty set answers `unknown`: nothing is not evidence of freshness.
    """
    order = {name: i for i, name in enumerate(FRESHNESS_RATINGS)}
    seen = [r for r in (str(x or "").strip() for x in ratings) if r in order]
    if not seen:
        return UNKNOWN
    return max(seen, key=lambda r: order[r])


def combined(ratings: Sequence[str]) -> str:
    """One rating for a whole projection, or `mixed` when they disagree.

    Not the same function as `worst()`, and the difference is the point: a
    projection whose fields are all `aging` IS `aging`, and one that is half
    `fresh` and half `stale` is `mixed` -- a word that means "look at the
    fields", not "average them". Collapsing that to `stale` would hide a usable
    field; collapsing it to `fresh` would hide a dangerous one.
    """
    seen = {r for r in (str(x or "").strip() for x in ratings)
            if r in FRESHNESS_RATINGS}
    if not seen:
        return UNKNOWN
    if len(seen) == 1:
        return seen.pop()
    return MIXED_FRESHNESS


def acceptable(level: Any) -> Tuple[str, ...]:
    """The ratings a risk level will accept (section 6.3).

    An unknown level accepts nothing. Failing closed here is deliberate: the
    argument comes from a consumer, and a typo that widened what counts as
    fresh enough would be invisible until it mattered.
    """
    return MINIMUM_FRESHNESS_LEVELS.get(str(level or "").strip(), ())


def meets(rating: Any, level: Any) -> bool:
    """Whether one rating satisfies one risk level."""
    return str(rating or "").strip() in acceptable(level)


def refresh_action(schema: Any, field: Any = "", *, source: str = "",
                   entity_id: str = "") -> Dict[str, Any]:
    """How to revalidate this field (section 1.3's `refresh_actions`).

    A description, never an execution. It names the adapter to ask and the TTL
    that would result, so a consumer can decide whether the refresh is worth
    its cost -- and so a route can offer the user a button instead of a shrug.
    """
    return {
        "entity_id": str(entity_id or ""),
        "schema": str(schema or ""),
        "field": str(field or ""),
        "source": str(source or ""),
        "ttl_seconds": ttl_for(schema, field),
        "how": "POST /api/state/refresh",
    }


def explain(rating: Any, *, observed_at: Any = "", schema: Any = "",
            field: Any = "", now: Any = None) -> str:
    """One sentence a person can read, with the numbers in it.

    Kept beside the arithmetic on purpose. A rating a user cannot interrogate
    is a rating they will either over-trust or ignore, and both failures look
    the same from here.
    """
    word = str(rating or "").strip()
    ttl = ttl_for(schema, field)
    age = age_seconds(observed_at, now=now)
    if word == UNKNOWN or age is None:
        return ("nothing has observed this, or the time it was observed cannot "
                "be read; it is not being presented as current")
    if word == FRESH:
        return (f"observed {age:.0f}s ago, inside the {ttl}s this field is "
                "guaranteed for")
    if word == AGING:
        return (f"observed {age:.0f}s ago, past its {ttl}s guarantee; fine for "
                "orientation, refresh before deciding on it")
    return (f"observed {age:.0f}s ago, more than {ttl * AGING_MULTIPLE:.0f}s; "
            "do not present this as the current state")


def policy_table() -> Dict[str, Dict[str, int]]:
    """Every field's TTL, for the diagnostics route and the settings screen.

    Built from `SCHEMA_FIELDS` rather than from `TTL_SECONDS`, so a field that
    was declared and never given a policy shows up with the floor instead of
    not showing up at all -- which is exactly the field worth noticing.
    """
    out: Dict[str, Dict[str, int]] = {}
    for schema, fields in SCHEMA_FIELDS.items():
        out[schema] = {name: ttl_for(schema, name) for name in fields}
    return out


def check_policy() -> Tuple[str, ...]:
    """Complaints about `TTL_SECONDS` itself. For a test, not for a route.

    Two ways this table goes wrong and neither raises at import: a key naming a
    schema or field that no longer exists (so the policy silently stops
    applying), and a non-positive TTL (which would make its field permanently
    stale). Both are invisible until somebody reads the numbers.
    """
    problems = []
    for key, value in sorted(TTL_SECONDS.items()):
        if not isinstance(value, int) or value <= 0:
            problems.append(f"{key}: a TTL must be a positive number of seconds, "
                            f"got {value!r}")
        schema, _, field = key.partition(".v1.")
        if not field:
            if key not in SCHEMA_FIELDS:
                problems.append(f"{key}: no schema by that name")
            continue
        schema_id = f"{schema}.v1"
        declared = SCHEMA_FIELDS.get(schema_id)
        if declared is None:
            problems.append(f"{key}: no schema {schema_id!r}")
        elif field not in declared:
            problems.append(f"{key}: {schema_id} does not declare {field!r}")
    return tuple(problems)
