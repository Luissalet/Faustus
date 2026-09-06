"""
context_engine/ranking.py — which candidate survives, and why the rest lost.

Retrieval hands this module a pile of `ContextCandidate`s from lanes that know
nothing about each other: a lexical hit, an embedding neighbour, a file the
caller named by path, a memory the curator promoted last week.  Ranking turns
that pile into an ordered, authorised, non-redundant list — and, just as much
of the job, into one `ContextOmission` for every candidate that did not make
it.  A packet that cannot say what it left out is a packet nobody can audit.

Two decisions here are not preferences.

**The score multiplies (§14.1).**

    final = task_fit x authority x freshness x source_validity
            x diversity x historical_utility

A weighted sum would let a source we can prove is invalid (`source_validity`
= 0) buy a slot because an embedding liked its wording.  That is not a ranking
bug — it is the exact failure this subsystem exists to prevent, and summing
turns the limit that was supposed to stop it into one vote among six.  A
product makes every factor a veto: any zero removes the candidate and no amount
of semantic similarity buys it back.  `historical_utility` is the one factor
allowed above 1.0 (to `HISTORICAL_UTILITY_MAX`), because "this actually got
used last time" *is* a vote and should be able to promote, not merely to fail
to demote.

**Authority is not relevance (§14.2).**  `AUTHORITY_ORDER` answers "when two
items disagree, which is current?".  It does not answer "which does this step
need?".  It appears in the product as a mild normalised factor — a user
instruction is a better thing to spend a slot on than an agent's guess — but
the question it actually decides is settled in `conflicts.py`, against the raw
ranks and never against this normalised factor.  Keeping the two apart is why a
high-authority irrelevancy does not outrank a low-authority answer.

Three smaller rules, each with a failure behind it:

* An unreadable or absent `observed_at` is *uncertainty*, not freshness.  It
  scores `FRESHNESS_UNKNOWN`, never 1.0.  An adapter emitting garbage stamps
  otherwise gets its candidates ranked as though observed this second, and the
  bug presents as "the model keeps citing something that changed yesterday".
* A `source_revision` that no longer matches the source is `stale` in
  `validate()`, not a soft penalty in `score()`.  Quoting a file we read at
  revision A while the workspace is at revision B is wrong, not merely old, and
  a wrong item that scores 0.7 still gets injected.
* Dedupe may never eat a contradiction (§22).  Two candidates with near
  identical wording and opposite verdicts are the most valuable pair in the
  packet, and a naive "same text, drop one" pass deletes exactly one of them.
  Every drop here is cleared through `conflicts.detect()` first.

Nothing in this module reads a clock except through `now`, opens a database or
calls a model, and nothing raises on the hot path: a candidate that cannot be
scored loses its slot, it does not take the turn down with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src import text_overlap
from src.memory import get_text_similarity

from . import conflicts
from .contracts import (
    AUTHORITY_ORDER,
    OMISSION_REASONS,
    TRUST_CLASSES,
    ContextCandidate,
    ContextOmission,
    ContextRequest,
)

logger = logging.getLogger(__name__)

# ── the factors ────────────────────────────────────────────────────────────

#: What a missing or unparseable `observed_at` is worth.  Penalised, because we
#: do not know; not zero, because not knowing is not the same as being wrong.
FRESHNESS_UNKNOWN = 0.6

#: Age never drives the factor to zero on its own: an old file is still the
#: file, and a product with a zero in it would delete it outright.
FRESHNESS_FLOOR = 0.15

#: Half-life in days, per source type.  A State Mirror projection is worthless
#: by tomorrow; a binding decision is not "half true" a month later, which is
#: why the durable kinds get a half-life longer than any session.  A ``delta``
#: sits beside ``objective`` and not beside ``state``, for the distinction this
#: table exists to make: a delta is the conclusion about two named, IMMUTABLE
#: revisions and stays true for as long as those two revisions exist — what
#: decays is its bearing on today's work, as newer revisions land on top of the
#: pair it compared.  At half a day every comparison older than about two days
#: would sit on `FRESHNESS_FLOOR`, which does not read as "this is old" but as
#: "this never happened".
FRESHNESS_HALF_LIFE_DAYS: Dict[str, float] = {
    "state": 0.5, "message": 1.0, "finding": 3.0, "web": 3.0,
    "file": 7.0, "symbol": 7.0, "capsule": 7.0, "artifact": 14.0,
    "objective": 30.0, "delta": 30.0, "memory": 45.0, "block": 60.0,
    "project_memory": 60.0,
    "document": 90.0, "experience": 90.0, "expert": 180.0, "recipe": 180.0,
    "decision": 3650.0, "instruction": 3650.0, "capability": 3650.0,
}
DEFAULT_HALF_LIFE_DAYS = 30.0

#: A lexical miss is not proof of irrelevance — Jaccard against a six-word
#: question misses half of what matters — so a derived `task_fit` bottoms out
#: here rather than at zero, which would collapse the product and throw away
#: the ordering the other five factors still carry.  An *explicit* score from a
#: reranker is honoured as given, zero included: that one is a statement.
TASK_FIT_FLOOR = 0.05

#: No query at all (a mandatory placement, a summarisation turn).  Everything
#: is equally on-topic, so the factor must not decide anything.
NEUTRAL_TASK_FIT = 0.5

#: The only factor allowed above 1.0.  1.3 is a promotion, not a rewrite of the
#: order: it cannot rescue a candidate whose validity or fit is near zero.
HISTORICAL_UTILITY_MAX = 1.3

#: A degraded source is one whose lane admitted it was running on one leg.
DEGRADED_PENALTY = 0.5

#: Content that has revisions but arrived without one: we cannot say *which*
#: version of the file this is, so it is worth slightly less than one we can.
UNPINNED_PENALTY = 0.9
REVISIONED_SOURCE_TYPES: Tuple[str, ...] = (
    "file", "symbol", "document", "artifact", "project_memory", "instruction",
)

#: Diversity is computed over a batch, so `score()` always reports 1.0 and
#: `rank()` is what applies it: the second chunk of one file is worth less than
#: the first, and the sixth is usually worth nothing at all.
DIVERSITY_REF_DECAY = 0.7
DIVERSITY_TYPE_DECAY = 0.9
DIVERSITY_TYPE_FREE = 3

#: Dedupe thresholds.  Jaccard catches reworded copies, the positional
#: `text_overlap` verifier catches long literal quotes inside longer texts.
DEDUPE_JACCARD = 0.9
DEDUPE_OVERLAP = 0.85

#: Two candidates that already share a `source_ref` and a revision need less
#: textual agreement to be called one thing said twice — but they still need
#: some.  Five chunks of one file share both fields and are five different
#: passages; dropping four of them as duplicates would delete the file's
#: contents and report it as tidiness.  Crowding is `diversify()`'s job.
DEDUPE_SAME_REF_JACCARD = 0.6

#: An adapter that reports its own confidence below this is telling us the item
#: is noise; taking it at its word is cheaper than ranking it.
MIN_CONFIDENCE = 0.05

DEFAULT_MAX_PER_REF = 2
DEFAULT_MAX_PER_TYPE = 6

#: Source kinds each policy flag gates.  Deliberately narrow: a flag that
#: blocks more than it names turns "incognito" into "no context at all".
PERSONAL_SOURCE_TYPES: Tuple[str, ...] = ("memory",)
PROJECT_SOURCE_TYPES: Tuple[str, ...] = (
    "project_memory", "file", "symbol", "instruction", "artifact",
)

#: Left out because the actor may still fetch it with a tool, against left out
#: because it is not theirs to see.  The agent is told the difference.
RECOVERABLE_OMISSION_REASONS: Tuple[str, ...] = (
    "budget", "duplicate", "stale", "contradicted", "irrelevant",
    "low_confidence",
)

_MIN_AUTHORITY_RANK = min(AUTHORITY_ORDER.values())
_MAX_AUTHORITY_RANK = max(AUTHORITY_ORDER.values())


@dataclass(frozen=True)
class ValidationResult:
    """Whether a candidate is allowed into the packet at all.

    `reason` is one of `OMISSION_REASONS` whenever `ok` is False, because it
    goes straight into the packet's omission list and a reason nobody can group
    by is a reason nobody reads."""

    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Scored:
    """One candidate with its final score and the six factors that made it.

    `parts` is kept rather than recomputed: a packet has to explain itself
    after the estimator, the profile and the thresholds have all moved on."""

    candidate: ContextCandidate
    final: float
    parts: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {"source_ref": self.candidate.source_ref,
                "source_type": self.candidate.source_type,
                "final": self.final, "parts": dict(self.parts)}


# ── small helpers ──────────────────────────────────────────────────────────

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:  # NaN: an unusable number is the floor, not a winner
        return low
    return max(low, min(high, number))


def _moment(now: Optional[datetime]) -> datetime:
    """The clock, injected or taken once.  A naive `now` is read as UTC rather
    than rejected: every caller in this repo passes UTC, and raising here would
    put an exception on the turn path over a timezone."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_ts(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001 - an unreadable stamp is unknown, not fatal
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _score_hint(candidate: ContextCandidate, *names: str) -> Optional[float]:
    """An explicit signal a source or a reranker attached, if it left one."""
    for name in names:
        try:
            if name not in candidate.scores:
                continue
            return float(candidate.scores[name])
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def _meta_flag(candidate: ContextCandidate, key: str) -> bool:
    try:
        return bool(candidate.meta.get(key))
    except Exception:  # noqa: BLE001 - a hostile meta may not be a Mapping
        return False


def _ref_key(candidate: ContextCandidate) -> str:
    return f"{candidate.source_type}:{candidate.source_ref}"


def _sort_key(item: "Scored") -> Tuple[float, int, str, str, str]:
    """A total order.  Ties broken all the way down to `candidate_id` so that
    two runs over the same input produce the same list, which is what Branching
    Futures compares when it claims two branches started from one context."""
    candidate = item.candidate
    return (-item.final, -candidate.authority_rank(), candidate.source_type,
            candidate.source_ref, candidate.candidate_id)


# ── validation ─────────────────────────────────────────────────────────────

def validate(candidate: ContextCandidate, request: ContextRequest, *,
             now: Optional[datetime] = None) -> ValidationResult:
    """May this candidate be in this actor's packet at all?

    Everything here is a hard gate, checked before anything is ranked: an
    unauthorised item that loses on score is still an unauthorised item that
    was read, scored and nearly injected."""
    ref = (candidate.source_ref or "").strip()
    if not ref:
        return ValidationResult(False, "unavailable", "candidate has no source_ref")
    if not (candidate.body or "").strip() and not (candidate.title or "").strip():
        return ValidationResult(False, "unavailable", "candidate has neither title nor body")

    owner = (request.execution.owner or "").strip()
    candidate_owner = (candidate.owner or "").strip()
    if owner and candidate_owner and candidate_owner != owner:
        return ValidationResult(False, "unauthorised",
                                f"owned by {candidate_owner!r}, request is {owner!r}")

    project = (request.execution.project_id or "").strip()
    candidate_project = (candidate.project_id or "").strip()
    if project and candidate_project and candidate_project != project:
        return ValidationResult(False, "unauthorised",
                                f"project {candidate_project!r} is not {project!r}")

    if _meta_flag(candidate, "quarantined"):
        return ValidationResult(False, "quarantined", "the source is quarantined")

    policy = request.policy
    if not policy.allow_personal_memory and candidate.source_type in PERSONAL_SOURCE_TYPES:
        return ValidationResult(False, "policy", "personal memory is off for this request")
    if not policy.allow_project_sources and candidate.source_type in PROJECT_SOURCE_TYPES:
        return ValidationResult(False, "policy", "project sources are off for this request")
    if (not policy.allow_semantic_lane and candidate.lanes
            and set(candidate.lanes) <= {"semantic"}):
        return ValidationResult(False, "policy",
                                "the semantic lane is off and no other lane found this")

    # Rule 3: a revision that moved is a wrong quote, not an old one.  The live
    # revision comes from the adapter that fetched the candidate, because this
    # layer is not allowed to touch the source itself.
    current = str((candidate.meta or {}).get("current_revision") or "").strip()
    if current and candidate.source_revision and current != candidate.source_revision:
        return ValidationResult(False, "stale",
                                f"read at {candidate.source_revision!r}, "
                                f"source is now {current!r}")

    confidence = _score_hint(candidate, "confidence")
    if confidence is not None and confidence < MIN_CONFIDENCE:
        return ValidationResult(False, "low_confidence",
                                f"source reports confidence {confidence:.3f}")

    minimum_freshness = policy.minimum_freshness_s
    if minimum_freshness is not None:
        observed = _parse_ts(candidate.observed_at)
        if observed is None:
            return ValidationResult(False, "stale",
                                    "no readable observed_at and this request "
                                    "requires a freshness guarantee")
        age = (_moment(now) - observed).total_seconds()
        if age > float(minimum_freshness):
            return ValidationResult(False, "stale",
                                    f"observed {int(age)}s ago, limit is "
                                    f"{minimum_freshness}s")

    return ValidationResult(True)


# ── scoring ────────────────────────────────────────────────────────────────

def _task_fit(candidate: ContextCandidate, request: ContextRequest) -> float:
    hint = _score_hint(candidate, "task_fit", "relevance", "rerank", "similarity")
    if hint is not None:
        return _clamp(hint)
    if set(candidate.lanes) & {"exact", "explicit", "mandatory"}:
        # Named by id, path or policy.  Nobody guessed this one was relevant.
        return 1.0
    query = (request.task.query or "").strip()
    if not query:
        return NEUTRAL_TASK_FIT
    try:
        similarity = get_text_similarity(query, f"{candidate.title}\n{candidate.body}")
    except Exception:  # noqa: BLE001 - a scorer may never break the turn
        return TASK_FIT_FLOOR
    return _clamp(similarity, TASK_FIT_FLOOR, 1.0)


def _authority(candidate: ContextCandidate) -> float:
    """`AUTHORITY_ORDER` normalised to [0, 1].

    An unrecognised label is treated as the weakest known rank, never as zero:
    a mislabelled candidate is a labelling bug, and a product would answer that
    bug by deleting the item and reporting nothing."""
    rank = AUTHORITY_ORDER.get(candidate.authority, _MIN_AUTHORITY_RANK)
    return _clamp(rank / float(_MAX_AUTHORITY_RANK))


def _freshness(candidate: ContextCandidate, moment: datetime) -> float:
    observed = _parse_ts(candidate.observed_at)
    if observed is None:
        return FRESHNESS_UNKNOWN
    half_life = FRESHNESS_HALF_LIFE_DAYS.get(candidate.source_type,
                                             DEFAULT_HALF_LIFE_DAYS)
    age_days = (moment - observed).total_seconds() / 86400.0
    if age_days <= 0:
        # Clock skew between a worker and the coordinator, not a prophecy.
        return 1.0
    return _clamp(0.5 ** (age_days / max(half_life, 0.01)),
                  FRESHNESS_FLOOR, 1.0)


def _source_validity(candidate: ContextCandidate) -> float:
    hint = _score_hint(candidate, "source_validity")
    if hint is not None:
        return _clamp(hint)
    value = TRUST_CLASSES.get(candidate.trust_class, 0.5)
    if candidate.degraded:
        value *= DEGRADED_PENALTY
    if (candidate.source_type in REVISIONED_SOURCE_TYPES
            and not (candidate.source_revision or "").strip()):
        value *= UNPINNED_PENALTY
    return _clamp(value)


def _historical_utility(candidate: ContextCandidate) -> float:
    hint = _score_hint(candidate, "historical_utility", "utility")
    if hint is None:
        return 1.0
    return _clamp(hint, 0.0, HISTORICAL_UTILITY_MAX)


def _product(parts: Mapping[str, float]) -> float:
    final = 1.0
    for value in parts.values():
        final *= float(value)
    return round(final, 9)


def score(candidate: ContextCandidate, request: ContextRequest, *,
          now: Optional[datetime] = None) -> Scored:
    """One candidate's six factors, multiplied.

    `diversity` is always 1.0 here: it is a property of a *batch*, and a single
    candidate has no crowd to be crowded out by.  `rank()` fills it in."""
    moment = _moment(now)
    parts: Dict[str, float] = {
        "task_fit": round(_task_fit(candidate, request), 6),
        "authority": round(_authority(candidate), 6),
        "freshness": round(_freshness(candidate, moment), 6),
        "source_validity": round(_source_validity(candidate), 6),
        "diversity": 1.0,
        "historical_utility": round(_historical_utility(candidate), 6),
    }
    return Scored(candidate=candidate, final=_product(parts), parts=parts)


def rank(candidates: Sequence[ContextCandidate], request: ContextRequest, *,
         now: Optional[datetime] = None) -> List[Scored]:
    """Score the batch, then charge each repeat of a source for the crowd it
    is joining, then sort.  Highest first, ties broken deterministically."""
    moment = _moment(now)
    base = [score(candidate, request, now=moment)
            for candidate in (candidates or ())
            if isinstance(candidate, ContextCandidate)]
    base.sort(key=_sort_key)

    seen_ref: Dict[str, int] = {}
    seen_type: Dict[str, int] = {}
    out: List[Scored] = []
    for item in base:
        ref, kind = _ref_key(item.candidate), item.candidate.source_type
        repeats_ref = seen_ref.get(ref, 0)
        repeats_type = seen_type.get(kind, 0)
        adjustment = DIVERSITY_REF_DECAY ** repeats_ref
        if repeats_type >= DIVERSITY_TYPE_FREE:
            adjustment *= DIVERSITY_TYPE_DECAY ** (repeats_type - DIVERSITY_TYPE_FREE + 1)
        seen_ref[ref] = repeats_ref + 1
        seen_type[kind] = repeats_type + 1
        parts = dict(item.parts)
        parts["diversity"] = round(_clamp(adjustment), 6)
        out.append(Scored(item.candidate, _product(parts), parts))
    out.sort(key=_sort_key)
    return out


# ── deduplication ──────────────────────────────────────────────────────────

def _duplicate_detail(winner: ContextCandidate,
                      challenger: ContextCandidate) -> Optional[str]:
    """Why `challenger` says nothing `winner` has not already said, or None."""
    same_source = bool(winner.source_ref) and (
        winner.source_ref == challenger.source_ref
        and winner.source_revision == challenger.source_revision)
    left, right = (winner.body or "").strip(), (challenger.body or "").strip()
    if not left or not right:
        return (f"same source_ref {winner.source_ref} at revision "
                f"{winner.source_revision or '(none)'}, with no body to tell "
                f"them apart") if same_source else None
    try:
        similarity = float(get_text_similarity(left, right))
        if same_source and similarity >= DEDUPE_SAME_REF_JACCARD:
            return (f"same source_ref {winner.source_ref} at revision "
                    f"{winner.source_revision or '(none)'}, saying the same thing")
        if similarity >= DEDUPE_JACCARD:
            return f"near-identical wording to {winner.source_ref}"
        ratio = float(text_overlap.overlap(left, right).get("ratio", 0.0))
    except Exception:  # noqa: BLE001 - a comparison may never break the turn
        return None
    if ratio >= DEDUPE_OVERLAP:
        return f"{int(ratio * 100)}% verbatim overlap with {winner.source_ref}"
    return None


def _contradicts(winner: ContextCandidate, challenger: ContextCandidate) -> bool:
    """Rule 4.  Similar text plus opposite claims is two facts, not one."""
    for conflict in conflicts.detect((winner, challenger)):
        if conflict.kind in conflicts.PROTECTS_FROM_DEDUPE:
            return True
    return False


def dedupe(scored: Sequence[Scored]) -> Tuple[List[Scored], List[Tuple[Scored, str]]]:
    """Drop candidates that repeat one already kept, highest score winning.

    Returns `(kept, [(dropped, detail)])`, both in score order.  The detail is
    prose for the omission, not a reason code: the code is always `duplicate`.

    The `_contradicts` check is the whole point of this function's shape.  A
    dedupe that only compares text removes one side of every disagreement it
    finds, and it removes it silently, which is the one failure mode the
    packet's omission list cannot describe after the fact."""
    kept: List[Scored] = []
    dropped: List[Tuple[Scored, str]] = []
    for item in sorted(scored, key=_sort_key):
        detail: Optional[str] = None
        for winner in kept:
            candidate_detail = _duplicate_detail(winner.candidate, item.candidate)
            if candidate_detail is None:
                continue
            if _contradicts(winner.candidate, item.candidate):
                logger.debug("context dedupe spared a contradiction: %s vs %s",
                             winner.candidate.source_ref, item.candidate.source_ref)
                continue
            detail = candidate_detail
            break
        if detail is None:
            kept.append(item)
        else:
            dropped.append((item, detail))
    return kept, dropped


def diversify(scored: Sequence[Scored], *, max_per_ref: int = DEFAULT_MAX_PER_REF,
              max_per_type: int = DEFAULT_MAX_PER_TYPE
              ) -> Tuple[List[Scored], List[Tuple[Scored, str]]]:
    """Cap how much of the packet one source, or one kind of source, may be.

    Five chunks of one file are not five pieces of evidence; they are one file,
    read five times, crowding out the test that would have contradicted it.
    The cap is applied after ranking, so what survives is the best of each
    source rather than whichever chunk the index happened to return first."""
    kept: List[Scored] = []
    dropped: List[Tuple[Scored, str]] = []
    per_ref: Dict[str, int] = {}
    per_type: Dict[str, int] = {}
    for item in sorted(scored, key=_sort_key):
        ref, kind = _ref_key(item.candidate), item.candidate.source_type
        if max_per_ref > 0 and per_ref.get(ref, 0) >= max_per_ref:
            dropped.append((item, f"{max_per_ref} pieces of {item.candidate.source_ref} "
                                  f"are already in the packet"))
            continue
        if max_per_type > 0 and per_type.get(kind, 0) >= max_per_type:
            dropped.append((item, f"{max_per_type} {kind} items are already in the packet"))
            continue
        per_ref[ref] = per_ref.get(ref, 0) + 1
        per_type[kind] = per_type.get(kind, 0) + 1
        kept.append(item)
    return kept, dropped


# ── the pipeline ───────────────────────────────────────────────────────────

def _omission(candidate: ContextCandidate, reason: str, detail: str,
              score_value: float = 0.0) -> ContextOmission:
    safe_reason = reason if reason in OMISSION_REASONS else "irrelevant"
    return ContextOmission(
        source_type=candidate.source_type,
        source_ref=candidate.source_ref,
        reason=safe_reason,
        score=round(float(score_value), 6),
        recoverable=safe_reason in RECOVERABLE_OMISSION_REASONS,
        detail=(detail or "")[:512],
    )


def select(candidates: Sequence[ContextCandidate], request: ContextRequest, *,
           now: Optional[datetime] = None,
           max_per_ref: int = DEFAULT_MAX_PER_REF,
           max_per_type: int = DEFAULT_MAX_PER_TYPE
           ) -> Tuple[List[Scored], List[ContextOmission]]:
    """validate -> dedupe -> conflicts.apply -> rank -> diversify.

    Returns what survives and exactly one `ContextOmission` per discard, with
    the reason the discard actually happened for.  "Exactly one" is an
    invariant worth stating: a candidate dropped at one stage never reaches the
    next, so no absence is explained twice and none goes unexplained.

    Conflicts are resolved *after* dedupe on purpose.  Dedupe removes the
    honest repetitions first, so the conflict pass sees each distinct claim
    once and does not spend its resolution on two copies of one side."""
    moment = _moment(now)
    omissions: List[ContextOmission] = []
    survivors: List[ContextCandidate] = []
    # Materialised once: the input is typed as a Sequence, but a caller that
    # passes a generator would otherwise find it consumed by the first pass and
    # empty by the time anything is counted.
    rows = [c for c in (candidates or ()) if isinstance(c, ContextCandidate)]

    for candidate in rows:
        verdict = validate(candidate, request, now=moment)
        if verdict.ok:
            survivors.append(candidate)
        else:
            omissions.append(_omission(candidate, verdict.reason, verdict.detail))

    kept, duplicates = dedupe([score(c, request, now=moment) for c in survivors])
    for item, detail in duplicates:
        omissions.append(_omission(item.candidate, "duplicate", detail, item.final))

    resolved, conflict_omissions = conflicts.apply([i.candidate for i in kept])
    omissions.extend(conflict_omissions)

    ranked = rank(resolved, request, now=moment)
    final, crowded = diversify(ranked, max_per_ref=max_per_ref,
                               max_per_type=max_per_type)
    for item, detail in crowded:
        omissions.append(_omission(item.candidate, "duplicate", detail, item.final))

    logger.debug("context select: %d in, %d out, %d omitted",
                 len(rows), len(final), len(omissions))
    return final, omissions


__all__ = [
    "FRESHNESS_UNKNOWN", "FRESHNESS_FLOOR", "FRESHNESS_HALF_LIFE_DAYS",
    "DEFAULT_HALF_LIFE_DAYS", "TASK_FIT_FLOOR", "NEUTRAL_TASK_FIT",
    "HISTORICAL_UTILITY_MAX", "DEGRADED_PENALTY", "UNPINNED_PENALTY",
    "REVISIONED_SOURCE_TYPES", "DIVERSITY_REF_DECAY", "DIVERSITY_TYPE_DECAY",
    "DIVERSITY_TYPE_FREE", "DEDUPE_JACCARD", "DEDUPE_OVERLAP",
    "DEDUPE_SAME_REF_JACCARD",
    "MIN_CONFIDENCE", "DEFAULT_MAX_PER_REF", "DEFAULT_MAX_PER_TYPE",
    "PERSONAL_SOURCE_TYPES", "PROJECT_SOURCE_TYPES",
    "RECOVERABLE_OMISSION_REASONS",
    "ValidationResult", "Scored",
    "validate", "score", "rank", "dedupe", "diversify", "select",
]
