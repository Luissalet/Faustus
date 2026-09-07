"""
contracts/event.py — the sealed envelope everything else reports through.

One vocabulary for progress, workflows, hooks, channels and observability, so
that the SSE stream a page listens to, the row an audit writes and the payload
a hook receives are the same object seen three times.  The names come straight
from the OpenHands reference:

    run.created → approval.requested → backend.started → tool.progress*
    → artifact.created* → run.completed | run.failed | run.cancelled

Redaction is part of the envelope, not a courtesy applied by whoever renders
it.  `redact()` removes secret values wherever they appear, and it *says how
many* it removed: a log line that quietly lost a field is indistinguishable
from one that never had it, and only one of those is safe to reason from.

The lesson from the two SSE dialects is baked in too: `sse()` emits an unnamed
frame, because a named frame never reaches `onmessage` and a page written
against the other endpoint goes deaf without erroring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, now_iso, reject_unknown,
    text, timestamp, whole,
)


#: Closed on purpose.  A name that nothing routes on is a string in a log; a
#: name in this list is something a hook, a page and an audit all understand.
EVENT_NAMES = (
    "run.created", "run.completed", "run.failed", "run.cancelled", "run.interrupted",
    "approval.requested", "approval.granted", "approval.denied", "approval.expired",
    "backend.started", "backend.finished",
    "tool.progress", "tool.blocked",
    "artifact.created", "artifact.discarded",
    "memory.proposed",
    "skill.installed", "skill.removed",
    "workflow.started", "workflow.node", "workflow.paused", "workflow.finished",
    # Project Context Links.  Underscored rather than dotted because they are
    # the names the Context Engine's cache already invalidates on
    # (`src/context_engine/cache.py::on_event`), and one spelling that two
    # subsystems agree on beats a prettier one that only half of them route.
    "project_context_attached", "project_context_detached",
    "project_context_updated", "project_context_refresh_queued",
    "project_context_indexed", "project_context_index_failed",
    "project_context_source_missing", "project_context_retrieved",
    # Council. Underscored for the same reason as the block above: it is the
    # spelling the plan uses and the one the context cache, the ledger and the
    # state mirror already route on. `council_message`, `council_turn_state`,
    # `council_usage` and `council_error` are the stream's own frames rather
    # than domain events, and they are listed here so that one vocabulary
    # covers what a page subscribes to and what an audit replays.
    "council_activity_started", "council_participant_resolved",
    "council_context_compiled", "council_claim_acquired",
    "council_claim_released", "council_objection_recorded",
    "council_task_handed_off", "council_decision_recorded",
    "council_activity_verified", "council_activity_completed",
    "council_activity_blocked", "council_message", "council_turn_state",
    "council_usage", "council_error",
    # The ledger's half of the same vocabulary. `CouncilLedger._emit` publishes
    # through `src/council/events.py`, whose `COUNCIL_EVENTS` must stay a subset
    # of this tuple: a name declared there and missing here would reach a page
    # and then be refused by the envelope an audit replays it through.
    "council_task_added", "council_task_assigned", "council_task_status",
    "council_claim_conflicted", "council_claim_handoff_refused",
    "council_claim_transferred", "council_objection_resolved",
    "council_decision_superseded",
    # State Mirror. Underscored for the same reason as the two blocks above:
    # it is the spelling the context cache and the council stream already
    # route on, and the dotted `state.changed` the plan writes in prose would
    # be a third dialect in a tuple that already carries two.
    # `src/state_mirror/events.py::STATE_EVENTS` must stay a SUBSET of this
    # tuple -- a name declared there and missing here would reach a page and
    # then be refused by the envelope an audit replays it through.
    "state_entity_discovered", "state_observation_received", "state_changed",
    "state_became_stale", "state_conflict_detected", "state_conflict_resolved",
    "state_entity_retired", "state_reconcile_started",
    "state_reconcile_completed", "state_source_degraded",
    "state_source_recovered", "state_error",
    # Universal Delta Engine. The plan writes these two ways -- §1.8 with an
    # underscore and §24 with a dot -- and this tuple already carries three
    # underscored blocks, so the dot loses. `src/delta_engine/events.py::
    # DELTA_EVENTS` must stay a SUBSET of this tuple, for the reason the two
    # comments above give: a name declared there and missing here reaches a
    # page and is then refused by the envelope an audit replays it through.
    # `delta_inconclusive` is separate from `delta_completed` on purpose: a
    # comparison that could not see enough to answer is not a comparison that
    # finished, and a consumer waiting for one should not be woken by the
    # other.
    "delta_requested", "delta_intent_compiled", "delta_source_resolved",
    "delta_extraction_completed", "delta_assertion_created",
    "delta_invariant_checked", "delta_regression_detected",
    "delta_coverage_computed", "delta_completed", "delta_inconclusive",
    "delta_reclassified", "delta_invalidated", "delta_error",
    # Greedy Completion Engine. Underscored like the four blocks above.
    # `src/completion_engine/events.py::COMPLETION_EVENTS` must stay a SUBSET
    # of this tuple, for the reason those comments give: a name declared there
    # and missing here reaches a page and is then refused by the envelope an
    # audit replays it through.
    #
    # `completion_converged` and `completion_budget_exhausted` are two names on
    # purpose and are never emitted for the same stop. They are the difference
    # between "there was nothing more worth doing" and "we ran out", which lead
    # to opposite next actions, and one event covering both would make the
    # second unreportable — which is how a budget that is too small stays too
    # small forever.
    "completion_contract_created", "completion_scope_compiled",
    "completion_layer_opened", "completion_candidate_discovered",
    "completion_candidate_rejected", "completion_batch_started",
    "completion_layer_verified", "completion_layer_completed",
    "completion_scope_expansion_requested", "completion_frontier_recomputed",
    "completion_converged", "completion_budget_exhausted",
    "completion_decision_recorded", "completion_error",
    # Modo Enséñame: demonstration capture and learned-procedure lifecycle.
    "teach_recording_started", "teach_observation_captured",
    "teach_recording_stopped", "teach_recording_cancelled",
    "teach_procedure_compiled", "teach_procedure_ready_for_replay",
    "teach_procedure_validated", "teach_procedure_needs_correction",
    "teach_procedure_approved", "teach_procedure_installed",
    "teach_procedure_established", "teach_procedure_deprecated",
    "teach_procedure_revoked", "teach_procedure_quarantined",
    # Immune System: health, incident containment and repair gates.
    "immune_asset_registered", "immune_health_assessed",
    "immune_failure_detected", "immune_capability_quarantined",
    "immune_repair_candidate_created", "immune_repair_certified",
    "immune_repair_canary", "immune_repair_promoted",
    "immune_repair_rejected", "immune_repair_rolled_back",
    # Branching Futures: isolated outcome lifecycle and real-state receipt.
    "branching_future_created", "branching_branch_started",
    "branching_branch_completed", "branching_evaluation_completed",
    "branching_branch_selected", "branching_future_committed",
    "branching_future_cancelled",
)

_REDACTED = "<redacted>"

#: Key names that carry a credential often enough that the value is dropped on
#: sight.  This is the cheap half; the exact-value pass below is the real one.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api[_-]?key|apikey|authorization|"
    r"auth|credential|cookie|session[_-]?key|private[_-]?key|refresh[_-]?token)"
    r"(?:$|_)", re.IGNORECASE,
)

#: Below this length a "secret" is more likely to be a substring of ordinary
#: prose than a credential, and blanking every "1" in a payload helps nobody.
_MIN_SECRET_LEN = 8


def _scrub(value: Any, secrets: Tuple[str, ...], hits: list, depth: int = 0) -> Any:
    """Walk a payload replacing secret values and secret-looking keys.  Every
    replacement appends to `hits`, so the caller can report the count instead
    of hoping the redaction happened."""
    if depth > 12:
        hits.append("depth")
        return _REDACTED
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                out[name] = _REDACTED
                hits.append(name)
            else:
                out[name] = _scrub(item, secrets, hits, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secrets, hits, depth + 1) for v in value]
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret and len(secret) >= _MIN_SECRET_LEN and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, _REDACTED)
                hits.append("value")
        return scrubbed
    return value


@dataclass(frozen=True)
class Event:
    """Immutable, ordered within its run, and redacted before it leaves."""

    name: str
    run_id: str = ""
    seq: int = 0
    at: str = ""
    owner: str = ""
    project_id: str = ""
    session_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    redactions: int = 0
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("name", "run_id", "seq", "at", "owner", "project_id", "session_id",
             "data", "redactions", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "event") -> "Event":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        name = text(data, "name", path, max_len=64)
        if name not in EVENT_NAMES:
            raise ContractError(
                path + ".name",
                f"is not a known event; add it to EVENT_NAMES if it is real, because "
                f"an unrouted name reaches no hook and no page. Known: {list(EVENT_NAMES)}",
                got=name,
            )
        payload = data.get("data")
        if payload is not None and not isinstance(payload, Mapping):
            raise ContractError(f"{path}.data", "expected an object", got=payload)
        return cls(
            name=name,
            run_id=text(data, "run_id", path, required=False, max_len=64),
            seq=whole(data, "seq", path, default=0, minimum=0),
            at=timestamp(data, "at", path, default=now_iso()),
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            session_id=text(data, "session_id", path, required=False, max_len=128),
            data=dict(payload or {}),
            redactions=whole(data, "redactions", path, default=0, minimum=0),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def redact(self, secrets: Tuple[str, ...] = ()) -> "Event":
        """Return the version safe to log, stream, export and hand to a hook.
        `redactions` counts what went: a consumer can tell "nothing sensitive
        was here" from "something was, and it is gone"."""
        hits: list = []
        scrubbed = _scrub(dict(self.data), tuple(secrets), hits)
        return Event(
            name=self.name, run_id=self.run_id, seq=self.seq, at=self.at,
            owner=self.owner, project_id=self.project_id, session_id=self.session_id,
            data=scrubbed, redactions=self.redactions + len(hits),
            schema_version=self.schema_version,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name, "run_id": self.run_id, "seq": self.seq, "at": self.at,
            "owner": self.owner, "project_id": self.project_id,
            "session_id": self.session_id, "data": dict(self.data),
            "redactions": self.redactions,
        }

    def sse(self) -> str:
        """An **unnamed** SSE frame.  Named frames never reach `onmessage`, and
        a page written against the unnamed dispatch stream goes silently deaf
        on a named one — that cost us a debugging session once already."""
        return "data: " + json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n\n"


def emit(name: str, *, run_id: str = "", seq: int = 0, secrets: Tuple[str, ...] = (),
         **payload: Any) -> Event:
    """Build a validated, already-redacted event in one call."""
    known = {"owner", "project_id", "session_id"}
    envelope = {k: payload.pop(k) for k in list(payload) if k in known}
    return Event.parse({
        "name": name, "run_id": run_id, "seq": seq, "data": payload, **envelope,
    }).redact(secrets)
