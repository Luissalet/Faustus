"""What an observation MEANS against the frozen intent. §16.

The adapter saw an element change. This module decides whether that was the
thing the user asked for, the price of the thing they asked for, a side effect,
or a bug -- and it is the only place in the package where the two axes of rule
4 (`operation` and `classification`) are allowed to meet. `modified` +
`requested` is success and `modified` + `regression` is a shipped bug: same
observation, opposite conclusion, and only the second needed the contract.

The order in `classify` is the design, not an implementation detail. Every
question is asked before every question that could mask it:

1. a violated `security` or `permissions` invariant, FIRST. §16: "seguridad y
   permisos tienen prioridad sobre similitud lingüística". A permission change
   whose path happens to resemble a requested one must never be filed as the
   thing the user asked for, and the only way to guarantee that is to answer
   before the resemblance is ever computed.
2. `scope.forbidden`, before the requested check and regardless of `allowed`.
   A forbidden path sitting inside the editable region is still forbidden; a
   classifier with only `allowed` reads it as requested, which is the case
   `ScopeRule` carries two fields to prevent.
3. only then, does an observation satisfy a `RequestedChange`.

Two rules have to be read as prohibitions rather than as steps:

* **`required` is never inferred from a name.** §16: "un cambio necesario
  necesita justificación trazable". If nobody supplied the justification, the
  change is not necessary -- it is incidental, and calling it `required`
  because its path looks related is the evaluator's own opinion wearing the
  vocabulary of evidence. The only justification this module accepts is
  `Finding.invariant_refs`, because that is the only one the frozen contract
  in `adapters/base.py` declares; there is no second channel and no
  `getattr(finding, "intent_refs", None)` for one, since a fallback on a field
  that should exist is a way of not finding out that it does not.

* **`unknown` never becomes `preserved`.** `DeltaAssertion.parse` would refuse
  the row anyway, and that refusal is a backstop, not the mechanism. Deciding
  it here keeps a contract rejection from being used as control flow, which is
  how the check ends up wrapped in a `try` and then quietly deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.delta_engine import confidence as confidence_mod
from src.delta_engine.adapters.base import Finding
from src.delta_engine.contracts import (
    CHANGE_OPERATIONS,
    DeltaAssertion,
    Invariant,
    IntentContract,
    InvariantResult,
    RequestedChange,
    ScopeRule,
    strongest_severity,
)

__all__ = ["Classified", "classify", "classify_all", "satisfies", "covers",
           "PATH_SEPARATORS"]

#: What ends a path segment. A prefix covers a path only when the next
#: character is one of these, so `a.b` covers `a.b.c` and `color` does NOT
#: cover `background_color`. Substring matching is the bug this tuple exists to
#: prevent: it would file a change to the background as the change to the
#: jacket the user asked for, and both the observation and the intent would
#: look correct in the row.
PATH_SEPARATORS: Tuple[str, ...] = (".", "/", "#", ":", "\\")

#: §16's first sentence, as data. A violation of one of these outranks every
#: later question, including a perfect match against a `RequestedChange`.
_SECURITY_CLASSES: Tuple[str, ...] = ("security", "permissions")


@dataclass(frozen=True)
class Classified:
    """Everything `classify_all` concluded, including what it did NOT find.

    `unmet` is the field that makes this more than a list: a `becomes(red)`
    that no observation satisfies is an errand nobody ran, and without it a
    delta full of successful assertions and one silently missing request looks
    exactly like a delta that did everything. It is the difference between
    `matched` and `mismatched` in §19, and it cannot be derived from
    `assertions` afterwards because the evidence for it is an ABSENCE.

    No `parse()`, for `AlignmentResult`'s reason: this is computed from a
    frozen intent and a set of findings, and a hand-written one would be a
    classification with no observation behind it.
    """

    assertions: Tuple[DeltaAssertion, ...] = ()
    unmet: Tuple[str, ...] = ()
    out_of_scope: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assertions": [a.to_dict() for a in self.assertions],
            "unmet": list(self.unmet),
            "out_of_scope": list(self.out_of_scope),
        }


def covers(prefix: str, path: str) -> bool:
    """Whether `prefix` addresses `path`: exactly, or as a parent segment.

    `a.b` covers `a.b.c` and `src` covers `src/auth.py#consume_state`. `color`
    covers neither `background_color` nor `colors`, because a prefix that does
    not end on a separator is a coincidence of spelling and the classifier
    would act on it as if the user had named the field.
    """
    left = str(prefix or "").strip()
    right = str(path or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    if not right.startswith(left):
        return False
    return right[len(left)] in PATH_SEPARATORS


def satisfies(change: RequestedChange, finding: Finding) -> bool:
    """Whether this observation is the thing this request asked for.

    Path first, always. An observation about an unrelated path satisfies
    nothing, and that guard is what keeps `absent` from being satisfied by
    every finding in the delta -- which would classify the whole comparison as
    `requested` and is the failure this ordering exists to prevent.

    `becomes` compares `after` to `value` under strip + casefold and nothing
    more: `Red` and `red ` are the same answer, and `#ff0000` is a different
    one that only the domain's adapter is in a position to reconcile. A
    normalizer here that understood colours would be this module deciding what
    a domain means, which is §3.1's line.

    `absent` is satisfied by an observation that is not an addition, so a path
    nobody observed at all satisfies nothing and lands in `unmet`. That reads
    harshly and is rule 1 again: unverified absence is not absence.
    """
    if not covers(change.path, finding.path):
        return False
    operation = finding.operation
    condition = change.condition
    if condition == "becomes":
        return (operation in ("modified", "added")
                and _normalized(finding.after) == _normalized(change.value))
    if condition == "changes":
        return operation in CHANGE_OPERATIONS
    if condition == "preserved":
        return operation == "unchanged"
    if condition == "added":
        return operation == "added"
    if condition == "removed":
        return operation == "missing"
    if condition == "absent":
        return operation != "added"
    return False


def _normalized(value: Any) -> str:
    """strip + casefold. The whole of `becomes`'s tolerance, written once."""
    return str(value or "").strip().casefold()


def classify(finding: Finding, *, intent: IntentContract,
             invariant_results: Sequence[InvariantResult] = ()
             ) -> Tuple[str, str, Tuple[str, ...]]:
    """`(classification, severity, intent_refs)` for one finding. §16's pipeline.

    The eight questions are asked in the order of the module docstring and the
    first one that answers wins. `intent_refs` holds the `RequestedChange`
    paths that justified the answer, and it is empty for every classification
    that was not justified by a request -- an empty tuple there is a claim in
    itself, since §16 requires a `required` or a `requested` to be traceable.
    """
    violations = _violations_for(finding, intent, invariant_results)
    effective = confidence_mod.propagate(finding.confidence, alignment=finding.alignment)
    relation, _ = confidence_mod.alignment_signal(finding.alignment)
    out_of_scope = _out_of_scope(finding.path, intent.scope)

    # 1. Security and permissions, before any resemblance is computed.
    for result, invariant in violations:
        if invariant is not None and invariant.klass in _SECURITY_CLASSES:
            return "regression", "blocking", ()

    # 2. A forbidden path is forbidden inside `allowed` too.
    if any(covers(rule, finding.path) for rule in intent.scope.forbidden):
        return "regression", _regression_severity(violations), ()

    # 3. What the user actually asked for -- except a `preserved` condition,
    # which step 5 owns. `satisfies` is true for one of those the moment the
    # observation is `unchanged`, so answering `requested` here would hand a
    # row whose confidence is `unknown` a word that sounds like success and
    # walk straight around the guard in step 5. Same fact, different label, and
    # the label is the whole protection.
    requested = _unique(change.path for change in intent.requested
                        if change.condition != "preserved" and satisfies(change, finding))
    if requested:
        return "requested", "info", requested

    # 4. Any other broken invariant.
    if violations:
        return "regression", _regression_severity(violations), ()

    # 5. A property that was watched and did not move.
    if finding.operation == "unchanged":
        held = _unique(change.path for change in intent.requested
                       if change.condition == "preserved" and covers(change.path, finding.path))
        watched = any(inv.path and covers(inv.path, finding.path)
                      for inv in intent.invariants)
        if held or watched:
            # Decided here rather than left to `DeltaAssertion.parse`: the
            # contract would reject the row, and a rejection used as a branch
            # is a rule that lives inside a `try` until someone deletes it.
            if effective == "unknown":
                return "unknown", "info", held
            return "preserved", "info", held

    # 6. A change somebody justified. Never a change that merely sounds related.
    if finding.invariant_refs:
        return "required", "info", ()

    # 7. A change the intent says nothing about.
    if finding.operation in CHANGE_OPERATIONS:
        if relation == "uncertain":
            # `incidental` would name a path; an uncertain alignment means the
            # path may belong to a different element entirely, so the honest
            # answer is that we do not know what changed, not that something
            # unrequested did.
            return "unknown", _collateral_severity(out_of_scope, "unknown"), ()
        if intent.scope.bounded or _at_least_medium(effective):
            return "incidental", _collateral_severity(out_of_scope, "incidental"), ()
        return "unknown", _collateral_severity(out_of_scope, "unknown"), ()

    # 8. Observed, and nothing in the frozen intent speaks to it.
    return "unknown", "info", ()


def classify_all(findings: Sequence[Finding], *, intent: IntentContract,
                 invariant_results: Sequence[InvariantResult] = ()) -> Classified:
    """Classify every finding, and name what nobody observed at all.

    The assertions are built by handing a payload to `DeltaAssertion.parse`
    rather than by constructing the dataclass, so the three refusals in that
    contract -- `preserved` with `unknown` confidence, `preserved` with a
    change operation, a confidence stronger than its tier -- apply to the rows
    this module produces exactly as they apply to rows arriving from anywhere
    else. A classifier that built its own assertions would be the one caller
    exempt from the contract it exists to satisfy.
    """
    collected = tuple(findings)
    assertions: List[DeltaAssertion] = []
    out_of_scope: List[str] = []

    for index, finding in enumerate(collected):
        classification, severity, intent_refs = classify(
            finding, intent=intent, invariant_results=invariant_results)
        violations = _violations_for(finding, intent, invariant_results)
        payload: Dict[str, Any] = dict(finding.to_dict())
        payload["classification"] = classification
        payload["severity"] = severity
        if intent_refs:
            payload["intent_refs"] = list(intent_refs)
        # Deduplicated because this list is CONCATENATED here, out of the
        # finding's own refs and the violations matched by path. A repeat is an
        # artifact of that join, and `text_list` rejects duplicates.
        refs = _unique(list(finding.invariant_refs)
                       + [result.invariant_id for result, _ in violations])
        if refs:
            payload["invariant_refs"] = list(refs)
        if payload.get("limitations"):
            payload["limitations"] = list(_unique(payload["limitations"]))
        assertions.append(DeltaAssertion.parse(payload, f"assertion[{index}]"))
        if finding.operation in CHANGE_OPERATIONS and _out_of_scope(finding.path, intent.scope):
            out_of_scope.append(finding.path)

    unmet = _unique(
        change.path for change in intent.requested
        if not any(satisfies(change, finding) for finding in collected)
    )
    return Classified(assertions=tuple(assertions), unmet=unmet,
                      out_of_scope=_unique(out_of_scope))


def _violations_for(finding: Finding, intent: IntentContract,
                    invariant_results: Sequence[InvariantResult]
                    ) -> Tuple[Tuple[InvariantResult, Optional[Invariant]], ...]:
    """The broken invariants that speak about THIS finding's path.

    Two ways to be about a finding, and no third: the finding names the
    invariant in `invariant_refs`, or the invariant names a path that covers
    the finding's. An invariant with neither -- no path and nobody referencing
    it -- applies to no single finding, and the alternative, letting every
    global violation land on every row, would turn one broken property into a
    delta where every observation reads as a regression and none of them says
    which one actually broke.

    `intent.invariant(id)` may answer `None` when a result arrives for an
    invariant the frozen contract never declared. That result is still a
    violation and is still reported; what it cannot do is claim the `security`
    floor in step 1, because the class it would claim it with is exactly what
    is missing.
    """
    out: List[Tuple[InvariantResult, Optional[Invariant]]] = []
    for result in invariant_results:
        if result.status != "violated":
            continue
        invariant = intent.invariant(result.invariant_id)
        named = result.invariant_id in finding.invariant_refs
        located = (invariant is not None and invariant.path
                   and covers(invariant.path, finding.path))
        if named or located:
            out.append((result, invariant))
    return tuple(out)


def _regression_severity(
        violations: Sequence[Tuple[InvariantResult, Optional[Invariant]]]) -> str:
    """The strongest severity in play, and never below `material`.

    `material` is a floor rather than a default because `DeltaAssertion.parse`
    refuses a regression filed as `info` -- "a regression that can be filed as
    `info` is a regression nobody will read" -- so computing anything weaker
    here would produce a payload the contract throws back, and the caller would
    read the exception as a bug in the contract rather than as this function
    under-rating a violation.
    """
    return strongest_severity([result.severity for result, _ in violations] + ["material"])


def _collateral_severity(out_of_scope: bool, classification: str) -> str:
    """How loudly an unrequested change asks to be read. §16: it may be either.

    A change outside the region the user drew is `material`: they said where
    the edit belonged and this is not there, and §12 forbids summarising a
    material row away. Inside the region, an `incidental` is `info` and an
    `unknown` is `minor` -- one is a side effect somebody can see the shape of,
    the other is a row where even the shape is missing, and a reader triaging
    three hundred assertions needs those two to sort differently.
    """
    if out_of_scope:
        return "material"
    return "minor" if classification == "unknown" else "info"


def _out_of_scope(path: str, scope: ScopeRule) -> bool:
    """Whether a path fell outside an editable region the request DID draw.

    False when the scope is unbounded, and that is not the same as "in scope":
    most code requests name no region at all, and treating an empty `allowed`
    as "nothing is allowed" would mark every observation in every unbounded
    delta as material and make the flag mean nothing.
    """
    if not scope.bounded:
        return False
    return not any(covers(rule, path) for rule in scope.allowed)


def _at_least_medium(level: str) -> bool:
    """Whether a confidence is `medium` or better. §16's step 7 threshold."""
    return confidence_mod.combine(level, "medium") == "medium"


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication of a list assembled here.

    Same rule as `coverage._unique`: this removes repeats that THIS module
    created by joining several sources, and leaves a duplicate inside a single
    caller's own list to be refused by `text_list`, where it is still that
    caller's contradiction.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text_value = str(value)
        if text_value in seen:
            continue
        seen.add(text_value)
        out.append(text_value)
    return tuple(out)
