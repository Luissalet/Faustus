"""One word about the change, and the sentence that has to accompany it. §19.

`ASSESSMENTS` is a five-word vocabulary about the CHANGE and `prove.VERDICTS`
is the authority about the RUN; `partial` is in both and means different things,
so nothing here ever assigns one to the other (rule 6 of `contracts.py`).

The order in `assess` is the whole module. Every rule exists because some
cheaper ordering produces a word that is technically defensible and reads as
reassurance:

* readability first, because a comparison with an unreadable end has not
  compared anything and every later rule would be reasoning from silence;
* **then "did we observe anything at all"**, which §19 numbers last. Last is
  where it cannot do its job: a delta with unmet requests and zero observations
  returns `mismatched` from the unmet rule and never reaches a tenth check.
  A delta that looked at nothing is not a delta that found nothing, and the
  only position where that sentence is true of every path through the function
  is immediately after the readability gate. This is the one deliberate
  departure from §19's numbering, and it is a departure in numbering only;
* regressions before achievements, because a delta that got the requested
  change AND broke a permission is not a success with a footnote;
* `unmet` before coverage, because an errand nobody ran is a stronger fact than
  a dimension nobody measured;
* `intent.unknowns` caps at `partial` unconditionally. A contract that could
  not translate part of the request into a checkable condition does not get to
  say `matched`, however well the part it did translate went.

`explain` is §35's sentence: what was achieved, what changed without being
asked, what was preserved, and what could not be checked. No adjectives -- the
words are the vocabulary of §4, and a reader who wants to know how bad it is
reads `severity`, which was computed from evidence rather than chosen for tone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.delta_engine.coverage import gaps as coverage_gaps
from src.delta_engine.coverage import sufficient
from src.delta_engine.contracts import (
    BLOCKING_SEVERITIES,
    Coverage,
    DeltaAssertion,
    IntentContract,
    InvariantResult,
    severity_rank,
)

__all__ = ["assess", "explain", "blocking_reasons", "needed_dimensions",
           "DIMENSION_FOR_CLASS"]

#: Which coverage axis an invariant of each class needs somebody to have
#: measured. Derived from the two closed vocabularies rather than from a field,
#: because `IntentContract` has no "dimensions I care about" and inventing one
#: would be a new contract; the classes a request actually declared are the
#: only honest evidence of which axes it needed.
#:
#: Partial on purpose. `security`, `permissions` and `budget` are checked by
#: invariant results and not by a coverage ratio, so mapping them to an axis
#: would make a delta `inconclusive` for failing to measure something no
#: adapter reports.
DIMENSION_FOR_CLASS: Dict[str, str] = {
    "identity": "identity",
    "provenance": "identity",
    "structure": "structural",
    "format": "structural",
    "content": "semantic",
    "behavior": "behavioral",
    "compatibility": "behavioral",
    "performance": "behavioral",
}


def needed_dimensions(intent: IntentContract) -> Tuple[str, ...]:
    """The coverage axes this intent's own invariants require somebody to read.

    An intent that named an `identity` invariant needs the `identity` axis to
    have been covered before anyone reports that identity held; with no ratio
    on that axis, "the face is unchanged" rests on nothing. Sorted so that two
    identical intents produce the same tuple and the assessment they drive is
    reproducible.
    """
    return tuple(sorted({
        DIMENSION_FOR_CLASS[inv.klass]
        for inv in intent.invariants
        if inv.klass in DIMENSION_FOR_CLASS
    }))


def assess(*, assertions: Sequence[DeltaAssertion], invariants: Sequence[InvariantResult],
           coverage: Coverage, intent: Optional[IntentContract] = None,
           unmet: Sequence[str] = ()) -> str:
    """One of `ASSESSMENTS`, by the order in the module docstring.

    `intent` is optional because a delta can be computed without a frozen
    contract -- "what changed between these two revisions" is a legitimate
    question. Without one there is nothing to be unmet and nothing to be
    unknown, so the rules that read it are skipped rather than defaulted; a
    default would be this function inventing an intent nobody froze.
    """
    if not coverage.both_readable:
        return "inconclusive"

    if not _observed_anything(assertions, invariants):
        return "inconclusive"

    # Blocking and material both answer `regressed`; the two checks stay apart
    # because `blocking_reasons` renders them differently and a caller deciding
    # whether to stop needs to know which one it is looking at.
    if any(result.blocking for result in invariants) or _regression_at(assertions, "blocking"):
        return "regressed"
    if _violated_at_least_material(invariants) or _regression_at_least_material(assertions):
        return "regressed"

    outstanding = tuple(unmet)
    if outstanding:
        achieved = any(a.classification == "requested" for a in assertions)
        return "partial" if achieved else "mismatched"

    if any(a.classification == "unknown" and a.severity in BLOCKING_SEVERITIES
           for a in assertions):
        return "partial"

    if intent is not None:
        if any(result.status == "unknown" and _named_by_the_request(result, intent)
               for result in invariants):
            return "partial"
        if intent.unknowns:
            return "partial"
        needed = needed_dimensions(intent)
        unmeasured = tuple(name for name in needed
                           if not sufficient(coverage, dimensions=(name,)))
        if unmeasured:
            # The only axis that mattered went unmeasured: there is no partial
            # answer to give, because nothing was established either way.
            return "inconclusive" if len(needed) == 1 else "partial"

    # Nothing was asked for, so there is nothing for the result to MATCH.
    #
    # "Compare these two and tell me what changed" is a legitimate question and
    # it produces a legitimate delta -- what it cannot produce is the word that
    # means "the request was fulfilled", because no request was made. Without
    # this rule a comparison that observed one incidental change came back
    # `matched`, which reads as "the job was done" to every consumer and to
    # every person scanning the list. `partial` and not `inconclusive`: the
    # comparison succeeded and its observations stand; it is the verdict about
    # the intent that is empty, not the evidence.
    #
    # `intent is None` is the same case reached from a caller that passed no
    # contract at all, and it gets the same answer for the same reason.
    if intent is None or not intent.requested:
        return "partial"

    return "matched"


def explain(assessment: str, *, assertions: Sequence[DeltaAssertion],
            invariants: Sequence[InvariantResult], coverage: Coverage,
            unmet: Sequence[str] = ()) -> str:
    """§35's sentence, in English, built from the rows and never stored.

    Four clauses in a fixed order -- achieved, unrequested, preserved, not
    checked -- because a reader who stops after the first sentence has to have
    stopped after the good news and not before the bad. The last clause is the
    one the whole subsystem is for: "no pude comprobar detalle fino del cabello
    por resolución insuficiente" is a sentence a system that conflated coverage
    with confidence cannot produce at all.
    """
    parts: List[str] = [f"Assessment: {assessment}."]

    requested = _with_classification(assertions, "requested")
    if requested:
        parts.append(f"{_count(len(requested), 'requested change')} observed: "
                     f"{_listed(a.path for a in requested)}.")
    else:
        parts.append("No requested change was observed.")

    outstanding = tuple(unmet)
    if outstanding:
        parts.append(f"{_count(len(outstanding), 'requested change')} with no "
                     f"observation that satisfies it: {_listed(outstanding)}.")

    regressions = _with_classification(assertions, "regression")
    if regressions:
        parts.append(f"{_count(len(regressions), 'regression')}: "
                     f"{_listed(a.path for a in regressions)}.")

    violated = tuple(r for r in invariants if r.status == "violated")
    if violated:
        parts.append(f"{_count(len(violated), 'invariant')} violated: "
                     f"{_listed(r.invariant_id for r in violated)}.")

    incidental = _with_classification(assertions, "incidental")
    if incidental:
        parts.append(f"{_count(len(incidental), 'change')} not requested: "
                     f"{_listed(a.path for a in incidental)}.")

    preserved = (len(_with_classification(assertions, "preserved"))
                 + len([r for r in invariants if r.status == "preserved"]))
    if preserved:
        parts.append(f"{_count(preserved, 'property')} preserved.")

    parts.append(_not_checked(assertions, invariants, coverage))
    return " ".join(part for part in parts if part)


def blocking_reasons(*, assertions: Sequence[DeltaAssertion],
                     invariants: Sequence[InvariantResult]) -> Tuple[str, ...]:
    """Everything a caller has to have read before accepting this change.

    The threshold is `BLOCKING_SEVERITIES`, the contract's own name for the set
    (`material`, `blocking`), read rather than restated here: a second
    definition of "what stops a caller" is a second answer, and §12's rule that
    a material row is never summarised away would then hold in one of them.

    Invariants come first and assertions follow, strongest severity first, for
    `material_assertions`' reason -- every consumer reads the first few lines
    of a long list, so which lines those are is decided here.
    """
    out: List[str] = []
    for result in invariants:
        if result.status == "violated" and result.severity in BLOCKING_SEVERITIES:
            out.append(f"invariant {result.invariant_id} is violated ({result.severity})")
    ordered = sorted(
        (a for a in assertions
         if a.classification == "regression" and a.severity in BLOCKING_SEVERITIES),
        key=lambda a: (-severity_rank(a.severity), a.path),
    )
    for assertion in ordered:
        out.append(f"regression at {assertion.path} ({assertion.severity})")
    return tuple(out)


def _not_checked(assertions: Sequence[DeltaAssertion],
                 invariants: Sequence[InvariantResult],
                 coverage: Coverage) -> str:
    """The clause naming what nobody measured. Empty only when nothing is missing.

    Assembled from three sources that are three different silences: an
    assertion nobody could classify, an invariant nobody could check, and a
    region nobody could read. Collapsing them into one number would be the
    single opaque score §3.5 refuses.
    """
    reasons: List[str] = []
    unknown = _with_classification(assertions, "unknown")
    if unknown:
        reasons.append(f"{_count(len(unknown), 'observation')} could not be classified")
    unchecked = tuple(r for r in invariants if r.status == "unknown")
    if unchecked:
        reasons.append(f"{_count(len(unchecked), 'invariant')} could not be checked: "
                       f"{_listed(r.invariant_id for r in unchecked)}")
    reasons.extend(coverage_gaps(coverage))
    if not reasons:
        return ""
    return "Not checked: " + "; ".join(reasons) + "."


def _with_classification(assertions: Sequence[DeltaAssertion],
                         name: str) -> Tuple[DeltaAssertion, ...]:
    return tuple(a for a in assertions if a.classification == name)


def _observed_anything(assertions: Sequence[DeltaAssertion],
                       invariants: Sequence[InvariantResult]) -> bool:
    """Whether anything was actually looked at. §19's tenth rule, asked early.

    An invariant result counts only when its status is not `unknown`: a result
    saying "I could not check this" is a record that a check was ATTEMPTED, and
    an attempt is not an observation. `not_applicable` does count -- deciding
    an invariant does not apply to a PNG is a conclusion somebody reached.
    """
    if assertions:
        return True
    return any(result.status != "unknown" for result in invariants)


def _regression_at(assertions: Sequence[DeltaAssertion], severity: str) -> bool:
    return any(a.classification == "regression" and a.severity == severity
               for a in assertions)


def _regression_at_least_material(assertions: Sequence[DeltaAssertion]) -> bool:
    return any(a.classification == "regression" and a.severity in BLOCKING_SEVERITIES
               for a in assertions)


def _violated_at_least_material(invariants: Sequence[InvariantResult]) -> bool:
    return any(r.status == "violated" and r.severity in BLOCKING_SEVERITIES
               for r in invariants)


def _named_by_the_request(result: InvariantResult, intent: IntentContract) -> bool:
    """Whether the USER named this invariant, rather than a default supplying it.

    §19's sixth rule distinguishes them because they cost different things: an
    unchecked `domain_default` is a gap in the profile, and an unchecked
    invariant the user asked for in their own words is a question they posed
    that came back unanswered, which cannot be reported as `matched`.
    """
    invariant = intent.invariant(result.invariant_id)
    return invariant is not None and invariant.source == "request"


def _count(number: int, noun: str) -> str:
    """`1 regression` / `2 regressions`. Plurals only, never an adjective."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _listed(values: Any, limit: int = 3) -> str:
    """At most `limit` names, then a count. A sentence nobody reads says nothing.

    The cap is on the SENTENCE and not on the delta: every path stays in
    `assertions`, and `blocking_reasons` and `material_assertions` are the
    calls that answer "show me all of them". A prose summary that pasted three
    hundred paths would be the summary §12 forbids in the other direction --
    unreadable, therefore unread, therefore acted past.
    """
    names = [str(value) for value in values]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", and {len(names) - limit} more"
