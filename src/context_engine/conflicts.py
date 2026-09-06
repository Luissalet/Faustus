"""
context_engine/conflicts.py — when two sources disagree, which one is current.

Ranking answers "which of these is worth a slot?".  This module answers a
question ranking cannot: "these two cannot both be true — which one does the
model get told?".  The two look like the same question and are not, and
conflating them produces the failure §22 of the plan names outright:

    a relevant contradiction must not be removed by dedupe.

The near-duplicate pass is a text comparison.  "The migration is safe to run"
and "the migration is not safe to run" are, to any lexical measure, the same
sentence — nine of ten tokens shared — so a dedupe that trusts similarity keeps
whichever arrived first and silently deletes the warning.  That is why
`ranking.dedupe()` clears every drop through `detect()` first, and why this
lives in its own module instead of as a helper inside the ranker.

What it may use to decide: an explicit subject and verdict an adapter recorded
(`meta["subject"]`, `meta["verdict"]`), the revision and observation time of
the source, the authority class, and a deliberately blunt polarity test over
the text.  What it may not use: a model.  A contradiction detector that asks an
LLM whether two sentences disagree is a detector that invents disagreements,
and this one runs on the turn path.

The blunt test, stated plainly so nobody has to reverse-engineer it: a negation
token anywhere in the text makes it negative; otherwise an affirmation token
makes it positive; anything else is neutral and claims nothing.  Negation beats
affirmation because "not safe" contains "safe", and scoring that pair as
neutral is exactly how the warning gets deleted.  It over-reports — "not risky"
against "safe" is flagged — and over-reporting costs one extra pair presented
side by side, which is the cheap direction for this error to run in.

Resolution is `AUTHORITY_ORDER` and nothing else, with one deliberate escape
hatch: when two contradicting items have the same authority *and* the same
trust class, both are kept.  A packet that shows the model two conflicting
statements is honest; a packet that picks one by coin flip and drops the other
is not, and nobody reading the manifest six months later could tell which
happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from src.memory import get_text_similarity, tokenize

from .contracts import ContextCandidate, ContextOmission

logger = logging.getLogger(__name__)

CONFLICT_KINDS: Tuple[str, ...] = (
    "contradiction",    # opposite claims about the same subject
    "superseded",       # the same source read at two revisions
    "stale_duplicate",  # the same source, same revision, read twice
    "authority",        # the same subject, settled by who said it
)

#: The kinds `ranking.dedupe()` must never treat as a duplicate.  A superseded
#: or twice-read source really is one thing said twice; a contradiction is two
#: things, and dropping either one of them is the bug this module exists for.
PROTECTS_FROM_DEDUPE: Tuple[str, ...] = ("contradiction", "authority")

#: Which `OMISSION_REASONS` entry a resolved conflict writes into the packet.
#: "contradicted" and "stale" are different stories and a reader acts on them
#: differently: one means "we chose a side", the other "you read an old copy".
CONFLICT_OMISSION_REASONS: Dict[str, str] = {
    "contradiction": "contradicted",
    "authority": "contradicted",
    "superseded": "stale",
    "stale_duplicate": "duplicate",
}

#: Lexical similarity above which two texts are considered to be *about* the
#: same thing, which is the precondition for reading their polarity as a
#: disagreement rather than as two unrelated statements.
CONTRADICTION_SIMILARITY = 0.5

#: Similarity above which two reads of one source at one revision are the same
#: text rather than two different chunks of it.
STALE_DUPLICATE_SIMILARITY = 0.8

#: How far apart two authority ranks must be before the gap alone decides a
#: same-subject disagreement.  20 is one full step in `AUTHORITY_ORDER`.
AUTHORITY_GAP = 20

#: Pair count is quadratic and this runs on the turn path.  The cap degrades
#: the answer (fewer conflicts found), never the call.
MAX_PAIRS = 4000

# Negation markers are syntactic and strong; affirmation markers are lexical
# and weak.  See the module docstring for why the asymmetry is deliberate.
_NEGATIONS = frozenset({
    "not", "no", "never", "cannot", "cant", "dont", "doesnt", "didnt", "isnt",
    "arent", "wasnt", "werent", "wont", "shouldnt", "without", "nor", "none",
    "neither", "fails", "failed", "failing", "failure", "broken", "breaks",
    "disabled", "false", "denied", "rejected", "unsupported", "unsafe",
    "invalid", "missing", "absent", "removed", "deprecated", "regression",
})
_AFFIRMATIONS = frozenset({
    "passes", "passed", "passing", "works", "working", "enabled", "true",
    "allowed", "supported", "safe", "valid", "present", "confirmed",
    "verified", "fixed", "succeeds", "succeeded", "green", "correct",
})


@dataclass(frozen=True)
class Conflict:
    """Two candidates that cannot both stand as written, and why we say so."""

    a: ContextCandidate
    b: ContextCandidate
    kind: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"a": self.a.source_ref, "b": self.b.source_ref,
                "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class Resolution:
    """Who wins, who loses, and whether losing means being left out at all.

    `keep_both=True` is not indecision — it is the recorded finding that
    neither side outranks the other, and that the model is better served by
    seeing the disagreement than by being handed one half of it."""

    winner_ref: str
    loser_ref: str
    keep_both: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"winner_ref": self.winner_ref, "loser_ref": self.loser_ref,
                "keep_both": self.keep_both, "reason": self.reason}


# ── reading a candidate ────────────────────────────────────────────────────

def _rows(candidates: Sequence[ContextCandidate]) -> List[ContextCandidate]:
    return [c for c in (candidates or ()) if isinstance(c, ContextCandidate)]


def _pairs(rows: Sequence[ContextCandidate]) -> Iterator[Tuple[int, int]]:
    seen = 0
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if seen >= MAX_PAIRS:
                return
            seen += 1
            yield i, j


def _meta_text(candidate: ContextCandidate, *keys: str) -> str:
    for key in keys:
        try:
            value = candidate.meta.get(key)
        except Exception:  # noqa: BLE001 - a hostile meta may not be a Mapping
            return ""
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _subject(candidate: ContextCandidate) -> str:
    """What the candidate is a claim *about*, when an adapter said so.

    Never guessed from the text: a subject inferred from wording would make
    every pair of sentences about files "the same subject"."""
    return _meta_text(candidate, "subject", "claim", "assertion_key")


def _verdict(candidate: ContextCandidate) -> str:
    return _meta_text(candidate, "verdict", "value", "answer")


def _polarity(candidate: ContextCandidate) -> int:
    """-1, 0 or +1.  Negation beats affirmation; see the module docstring."""
    explicit = _verdict(candidate)
    words = set(tokenize(f"{candidate.title} {candidate.body} {explicit}".lower()))
    if words & _NEGATIONS:
        return -1
    if words & _AFFIRMATIONS:
        return 1
    return 0


def _observed(candidate: ContextCandidate) -> Optional[datetime]:
    raw = (candidate.observed_at or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001 - an unreadable stamp is simply unknown
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _newer(a: ContextCandidate, b: ContextCandidate) -> Optional[ContextCandidate]:
    """The later of two candidates, or None when nothing proves which is later.

    Revision strings are hashes as often as they are numbers, so they are never
    compared for order — only timestamps are."""
    at, bt = _observed(a), _observed(b)
    if at is None or bt is None or at == bt:
        return None
    return a if at > bt else b


def _similar(a: ContextCandidate, b: ContextCandidate) -> float:
    try:
        return float(get_text_similarity(f"{a.title} {a.body}", f"{b.title} {b.body}"))
    except Exception:  # noqa: BLE001 - a detector may never break its caller
        return 0.0


# ── detection ──────────────────────────────────────────────────────────────

def _classify(a: ContextCandidate, b: ContextCandidate) -> Optional[Conflict]:
    """The one conflict kind a pair is, or None.  Order of the checks is the
    order of certainty: recorded facts first, wording last."""
    same_ref = bool(a.source_ref) and a.source_ref == b.source_ref

    if same_ref and a.source_revision != b.source_revision:
        return Conflict(a, b, "superseded",
                        f"{a.source_ref} at revision "
                        f"{a.source_revision or '(none)'} and "
                        f"{b.source_revision or '(none)'}")

    subject_a, subject_b = _subject(a), _subject(b)
    same_subject = bool(subject_a) and subject_a == subject_b
    verdict_a, verdict_b = _verdict(a), _verdict(b)
    if same_subject and verdict_a and verdict_b and verdict_a != verdict_b:
        return Conflict(a, b, "contradiction",
                        f"subject {subject_a!r}: {verdict_a!r} against {verdict_b!r}")

    similarity = _similar(a, b)
    if same_subject or similarity >= CONTRADICTION_SIMILARITY:
        pa, pb = _polarity(a), _polarity(b)
        if pa and pb and pa != pb:
            return Conflict(a, b, "contradiction",
                            f"same statement (similarity {similarity:.2f}), "
                            f"opposite polarity")

    if same_subject and abs(a.authority_rank() - b.authority_rank()) >= AUTHORITY_GAP:
        return Conflict(a, b, "authority",
                        f"subject {subject_a!r} asserted by {a.authority} "
                        f"and {b.authority}")

    if (same_ref and a.source_revision == b.source_revision
            and similarity >= STALE_DUPLICATE_SIMILARITY
            and a.observed_at != b.observed_at):
        return Conflict(a, b, "stale_duplicate",
                        f"{a.source_ref} read twice at the same revision")

    return None


def detect(candidates: Sequence[ContextCandidate]) -> List[Conflict]:
    """Every pair that cannot stand as written, in input order.

    Deliberately conservative in one direction only: a pair reported here that
    turns out to agree costs one extra item in the packet, while a pair missed
    here costs the packet the half of a disagreement nobody will notice is
    gone."""
    rows = _rows(candidates)
    out: List[Conflict] = []
    for i, j in _pairs(rows):
        conflict = _classify(rows[i], rows[j])
        if conflict is not None:
            out.append(conflict)
    return out


# ── resolution ─────────────────────────────────────────────────────────────

def _resolve_pair(
    conflict: Conflict,
) -> Tuple[ContextCandidate, ContextCandidate, Resolution]:
    """The winner, the loser and the finding.

    `apply()` needs the objects, not the refs: `superseded` pairs share a
    `source_ref` by definition, so a resolution identified only by ref cannot
    say which of the two it meant."""
    a, b = conflict.a, conflict.b

    if conflict.kind in ("superseded", "stale_duplicate"):
        newer = _newer(a, b)
        if newer is None:
            return a, b, Resolution(
                a.source_ref, b.source_ref, True,
                "two reads of one source and no timestamp says which is later")
        older = b if newer is a else a
        return newer, older, Resolution(
            newer.source_ref, older.source_ref, False,
            f"observed {newer.observed_at} supersedes {older.observed_at}")

    rank_a, rank_b = a.authority_rank(), b.authority_rank()
    if rank_a != rank_b:
        winner, loser = (a, b) if rank_a > rank_b else (b, a)
        return winner, loser, Resolution(
            winner.source_ref, loser.source_ref, False,
            f"{winner.authority} outranks {loser.authority}")

    trust_a, trust_b = a.trust(), b.trust()
    if trust_a != trust_b:
        winner, loser = (a, b) if trust_a > trust_b else (b, a)
        return winner, loser, Resolution(
            winner.source_ref, loser.source_ref, False,
            f"equal authority; {winner.trust_class} is trusted above "
            f"{loser.trust_class}")

    newer = _newer(a, b)
    if newer is not None:
        older = b if newer is a else a
        return newer, older, Resolution(
            newer.source_ref, older.source_ref, True,
            "equal authority and trust: both presented, newer first")
    return a, b, Resolution(a.source_ref, b.source_ref, True,
                            "equal authority and trust: both presented")


def resolve(conflict: Conflict) -> Resolution:
    """Who is presented as current.  Authority first, trust second, time third,
    and a tie means both survive rather than one being chosen arbitrarily."""
    return _resolve_pair(conflict)[2]


def _omission(loser: ContextCandidate, kind: str, reason: str,
              winner: ContextCandidate) -> ContextOmission:
    return ContextOmission(
        source_type=loser.source_type,
        source_ref=loser.source_ref,
        reason=CONFLICT_OMISSION_REASONS.get(kind, "contradicted"),
        score=0.0,
        # The loser still exists and can still be opened with a tool.  That is
        # the entire difference between leaving something out and hiding it.
        recoverable=True,
        detail=f"{kind}: {reason} (kept {winner.source_ref})"[:512],
    )


def apply(
    candidates: Sequence[ContextCandidate],
) -> Tuple[List[ContextCandidate], List[ContextOmission]]:
    """Resolve every conflict and return the survivors plus one omission per
    candidate actually dropped.

    A candidate that has already lost a conflict takes no further part: losing
    twice would emit two omissions for one absence, and the packet's accounting
    is only worth something if it is exact."""
    rows = _rows(candidates)
    if len(rows) < 2:
        return list(rows), []

    losers: Dict[int, Tuple[str, str, ContextCandidate]] = {}
    for i, j in _pairs(rows):
        if i in losers or j in losers:
            continue
        conflict = _classify(rows[i], rows[j])
        if conflict is None:
            continue
        _winner, loser, resolution = _resolve_pair(conflict)
        if resolution.keep_both:
            logger.debug("context conflict kept both: %s / %s (%s)",
                         rows[i].source_ref, rows[j].source_ref, conflict.kind)
            continue
        losers[j if loser is conflict.b else i] = (
            conflict.kind, resolution.reason, _winner)

    kept = [row for n, row in enumerate(rows) if n not in losers]
    omissions = [_omission(rows[n], kind, reason, winner)
                 for n, (kind, reason, winner) in sorted(losers.items())]
    return kept, omissions


__all__ = [
    "CONFLICT_KINDS", "PROTECTS_FROM_DEDUPE", "CONFLICT_OMISSION_REASONS",
    "CONTRADICTION_SIMILARITY", "STALE_DUPLICATE_SIMILARITY", "AUTHORITY_GAP",
    "Conflict", "Resolution", "detect", "resolve", "apply",
]
