"""The closing report: what was asked for, what was added, and what was not done.

§32's five lines, in English, built from a `CompletionDecision` and never from
a narrative. The shape is fixed:

    Core: ...
    Extras: ...
    Not done: ...
    Proof: ...
    Stop reason: ...

with one more line, `Not consulted:`, whenever an integration was dark.

Five rules are mechanised here, each with the failure it prevents:

1.  **An extra is never folded into the core summary.** §30. `Closeout.extras`
    is `CompletionDecision.extras()` -- the contract's own method, `bonus` and
    `exploratory` -- so the receipt and the record cannot disagree about what
    counted as unrequested. And the Extras line is printed EVEN WHEN EMPTY, as
    `Extras: none`, because an absent line reads as "there were none" while an
    empty one reads as "we checked". Those are different claims and only one of
    them is being made.

2.  **"Not done" says why.** Every entry carries its `REJECTION_REASONS` word
    turned into a sentence, from `REASON_PHRASES`, which a test asserts covers
    the vocabulary exactly. §1.8's rule is that a rejection is recorded; a
    recorded rejection with no reason comes back next round anyway.

3.  **A stop for budget is never written as convergence.** `CompletionDecision.
    parse` already refuses the combination, so the record cannot hold the lie
    -- but a renderer could still print two exhausted budget lines under a
    sentence that reads like "there was nothing left worth doing". `STOP_
    PHRASES` gives the two openings that share no wording, and the `budget`
    phrase names the lines that ran out and says to raise them, because that is
    the next action the reader has to be able to take.

4.  **A dark integration is disclosed.** `CompletionDecision.
    degraded_integrations` is printed. A run that could not reach the Delta
    Engine did not see scope creep, and a close that omits that is claiming to
    know more than it knows.

5.  **The delta's own findings are separated the same way.** Incidental changes
    and scope creep are printed as EXTRAS -- an unasked change on disk is the
    purest extra there is -- and a regression the delta saw goes to `Not done`
    with a phrase of its own, deliberately not one of `REJECTION_REASONS`,
    because nobody rejected it: it was observed and left.

Nothing here recomputes a decision. This module formats; the frontier, the
budget and `prove` decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.completion_engine import budgeting
from src.completion_engine.adapters import delta_engine as delta_adapter
from src.completion_engine.adapters import proof as proof_adapter
from src.completion_engine.contracts import (
    HONEST_STOPS,
    REJECTION_REASONS,
    REQUIRED_LAYERS,
    STOP_REASONS,
    CompletionContract,
    CompletionDecision,
)

__all__ = [
    "Closeout",
    "REASON_PHRASES",
    "STOP_PHRASES",
    "DEFERRED_PHRASE",
    "OBSERVED_REGRESSION_PHRASE",
    "unphrased",
    "build",
    "render",
    "summary_line",
]

#: `REJECTION_REASONS` -> the sentence a person reads. Closed and complete: a
#: test compares the key set against the vocabulary, because a reason with no
#: phrase would print as a bare token in the one line of the report whose whole
#: job is to explain itself.
REASON_PHRASES: Dict[str, str] = {
    "out_of_scope": "outside the scope envelope",
    "no_permission": "the run does not hold the permission it would need",
    "forbidden_effect": "the effect it needs is denied here",
    "below_threshold": "below the marginal value bar",
    "dominated": "another candidate was better on every axis",
    "duplicate": "already covered by something that ran",
    "resolved": "something else fixed it on the way past",
    "quarantined": "it depends on a capability that is not healthy",
    "budget": "it would not fit in the budget",
    # Not the same sentence as `below_threshold`, and the difference matters to
    # whoever reads this next: these cleared the bar and were wanted, and the
    # only thing that stopped them was that this round had already taken its
    # batch. Filing them under "not worth doing" would lose that.
    "round_full": "it was wanted and this round had already taken its batch",
    "risk": "a blocking risk, whatever its value",
    "stale": "its evidence aged out and did not revalidate",
    "superseded": "the state it was about has moved",
    # Not "we decided against it". Every other sentence in this table reports a
    # judgement this engine made; this one reports being told, and a closeout
    # that blurred the two would let the engine take credit for a decision it
    # was handed.
    "declined": "you said no to it, and it will not be offered again",
}

#: A deferred candidate may carry no reason -- `ImprovementCandidate.parse`
#: allows that, because `deferred` already means "worth doing, not now" and §19
#: hands those to the Opportunity Engine rather than throwing them away.
DEFERRED_PHRASE = "worth doing; handed on rather than done in this run"

#: Deliberately NOT one of `REASON_PHRASES`. A regression the Delta Engine saw
#: was not refused by anybody -- it was observed and left -- and printing it
#: under a rejection reason would invent a decision nobody made.
OBSERVED_REGRESSION_PHRASE = (
    "a regression the delta engine observed; nothing in this run recorded "
    "repairing it")

#: `STOP_REASONS` -> the sentence a person reads. `converged` and `budget` are
#: the pair rule 3 is about: they share no wording, and the budget phrase names
#: the next action. Everything else is an interruption and is worded as one.
STOP_PHRASES: Dict[str, str] = {
    "converged": "nothing left cleared the marginal value bar",
    # Neither of the two above, and that is the point of the word. The turn
    # ended -- the model stopped calling tools -- while work this mode says to
    # do was still admitted and still affordable. Saying "converged" here would
    # claim there was nothing left; saying "budget" would claim we ran out.
    "unfinished": "the turn ended while work this mode calls for was still "
                  "open, and neither the budget nor the scope had stopped it",
    "budget": "a budget line ran out while work remained that had not been "
              "ruled out; raise it to see the rest",
    "scope": "the next useful thing was outside the scope envelope",
    "risk": "a blocking risk stopped further work",
    "blocked": "something outside this run has to happen first",
    "user": "a person said stop",
    "cancelled": "the run was cancelled",
    "core_only": "the mode asked for the core alone, and nothing beyond it was "
                 "opened",
    "failed": "the core did not complete",
}


def unphrased() -> Dict[str, Tuple[str, ...]]:
    """Vocabulary words this module has no sentence for. Empty is the contract.

    A public check rather than a module-level assertion, because this module is
    on the turn path and `contracts.py` states the rule it follows: a module
    here never fails to import over a vocabulary lookup. So the tables fall
    back to the raw token at render time -- ugly, and readable -- while this
    function is what a test asserts against, and what tells whoever adds a
    thirteenth `REJECTION_REASONS` word exactly which line of the report would
    have printed it bare.
    """
    return {
        "rejection_reasons": tuple(name for name in REJECTION_REASONS
                                   if name not in REASON_PHRASES),
        "stop_reasons": tuple(name for name in STOP_REASONS
                              if name not in STOP_PHRASES),
    }


@dataclass(frozen=True)
class Closeout:
    """One run's closing report, already separated into its four lists.

    The lists are TEXT, not candidates, because this is the artefact a person
    reads and the candidates are still on the decision it carries. Anything
    that wants the structure asks `closeout.decision`; anything that wants the
    sentence reads the list.
    """

    decision: CompletionDecision
    core: Tuple[str, ...] = ()
    extras: Tuple[str, ...] = ()
    not_done: Tuple[str, ...] = ()
    proof: str = ""
    stop: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """The report as data. `decision` is its own `summary()`, not a copy.

        Reusing `CompletionDecision.summary()` rather than restating the counts
        here is the difference between one set of numbers and two that agree
        until somebody adds a layer.
        """
        return {
            "decision_id": self.decision.id,
            "mode": self.decision.mode,
            "core": list(self.core),
            "extras": list(self.extras),
            "not_done": list(self.not_done),
            "proof": self.proof,
            "stop": self.stop,
            "stop_reason": self.decision.stop_reason,
            "honest_stop": self.decision.stop_reason in HONEST_STOPS,
            "degraded_integrations": list(self.decision.degraded_integrations),
            "decision": self.decision.summary(),
            "rendered": render(self),
        }


def _titles(candidates: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for candidate in candidates or ():
        title = str(getattr(candidate, "title", "") or "").strip()
        if title and title not in out:
            out.append(title)
    return out


def _reason_phrase(candidate: Any) -> str:
    """The sentence for one refused or deferred candidate.

    A `rejected` candidate always has a reason -- `ImprovementCandidate.parse`
    refuses one without -- so the fallback here is only ever reached by a
    `deferred` row, which is allowed to have none.
    """
    reason = str(getattr(candidate, "rejection_reason", "") or "")
    if reason:
        return REASON_PHRASES.get(reason, reason)
    return DEFERRED_PHRASE


def _not_done(decision: CompletionDecision,
              delta: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Every piece of work that did not happen, each with why it did not.

    Rejected first, then deferred, then the regressions the delta saw. The
    order is the order of increasing surprise: a rejection was a decision, a
    deferral was a decision to decide later, and an unrepaired regression was
    neither -- it is the one entry the reader is least likely to expect and it
    reads best last, where nothing follows it.
    """
    out: List[str] = []
    seen = set()
    for candidate in tuple(decision.rejected) + tuple(decision.deferred):
        title = str(getattr(candidate, "title", "") or "").strip()
        if not title:
            continue
        entry = f"{title} ({_reason_phrase(candidate)})"
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    for row in delta_adapter.regressions(delta):
        where = str(row.get("path") or row.get("id") or "").strip()
        if not where:
            continue
        entry = f"{where} ({OBSERVED_REGRESSION_PHRASE})"
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return tuple(out)


def _extras(decision: CompletionDecision,
            delta: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """What was added beyond the request, from the decision and from the delta.

    `decision.extras()` is the authority for candidates -- the contract's own
    method, so no second definition of "extra" can appear here. The delta adds
    the two kinds of unrequested change that never became a candidate at all: a
    change it saw and nobody asked for, and a change outside the intent's
    scope. Both are labelled, because a reader has to be able to tell an extra
    somebody chose from an extra somebody noticed.
    """
    out: List[str] = _titles(decision.extras())
    seen = set(out)
    for row in delta_adapter.incidental(delta):
        where = str(row.get("path") or row.get("id") or "").strip()
        entry = f"{where} (an unasked change the delta engine saw)" if where else ""
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    for item in delta_adapter.scope_creep(delta):
        entry = f"{item} (outside the intent's scope)"
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return tuple(out)


def _proof_text(decision: CompletionDecision,
                proof: Optional[Mapping[str, Any]]) -> str:
    """The proof line's content: a verdict, and the heaviest doubt behind it.

    `proved` prints alone because there is no doubt heavy enough to name -- and
    every other verdict prints WITH its doubt, because §19's rule is that the
    close distinguishes a proved core from a partial bonus, and the two words
    on their own do not do that.

    No proof at all is "not established", which is deliberately not `unproved`.
    `unproved` is a verdict somebody computed from evidence; "not established"
    is the absence of one, and a run whose `proof_refs` are empty too has not
    even pointed at a proof somebody else holds.
    """
    verdict = proof_adapter.verdict_of(proof)
    if not verdict:
        if decision.proof_refs:
            return ("not established here; the run points at "
                    f"{len(decision.proof_refs)} proof reference(s)")
        return "not established: nothing recorded a verdict for this run"
    if verdict == "proved":
        return "proved"
    doubt = proof_adapter.uncertainty_line(proof)
    return f"{verdict} — {doubt}" if doubt else verdict


def _stop_text(decision: CompletionDecision) -> str:
    """Why the run stopped, in words that cannot be mistaken for another stop.

    The `budget` phrase is extended with the lines that actually ran out, from
    `budgeting.exhausted_lines` -- the same function the engine uses to decide,
    so the receipt names the same lines the decision was made on. "A budget
    line ran out" without saying which one is a sentence the reader cannot act
    on, and acting on it is the whole reason rule 3 keeps this apart from
    convergence.

    An unrecognised `stop_reason` cannot reach here from `parse`, which uses
    `one_of`; the fallback exists for a `CompletionDecision` built by hand and
    says plainly that the reason is unknown rather than inventing one.
    """
    reason = str(decision.stop_reason or "")
    phrase = STOP_PHRASES.get(reason, f"stop reason `{reason or 'unset'}` is not "
                                      f"one this report has words for")
    if reason == "budget":
        try:
            lines = budgeting.exhausted_lines(decision.budget)
        except Exception:  # noqa: BLE001 - a receipt never fails over arithmetic
            lines = ()
        if lines:
            phrase = f"{phrase} (exhausted: {', '.join(lines)})"
    detail = str(decision.stop_detail or "").strip()
    return f"{phrase} — {detail}" if detail else phrase


def build(decision: Any, *, contract: Optional[CompletionContract] = None,
          proof: Optional[Mapping[str, Any]] = None,
          delta: Optional[Mapping[str, Any]] = None) -> Closeout:
    """Assemble the report. Nothing is decided here, only separated.

    `decision` may be a `CompletionDecision` or the mapping one serialises to;
    a mapping goes through `CompletionDecision.parse`, which is where rule 3's
    refusal lives -- so a caller cannot hand this function a hand-built dict
    claiming `converged` with an exhausted budget and get a clean report out.

    `contract` is optional and is read for exactly one thing, in
    `_unmet_promise`: the core deliverables it recorded before the work
    started. It is NEVER used to fill the core line, because printing the
    promise where the reader expects the result is the precise inversion this
    module exists to prevent.
    """
    record = decision if isinstance(decision, CompletionDecision) \
        else CompletionDecision.parse(decision, "decision")
    core: List[str] = []
    for layer in REQUIRED_LAYERS:
        for title in _titles(record.by_layer(layer)):
            if title not in core:
                core.append(title)
    not_done = _not_done(record, delta) + _unmet_promise(record, contract)
    return Closeout(
        decision=record,
        core=tuple(core),
        extras=_extras(record, delta),
        not_done=not_done,
        proof=_proof_text(record, proof),
        stop=_stop_text(record),
    )


def _unmet_promise(decision: CompletionDecision,
                   contract: Optional[CompletionContract]) -> Tuple[str, ...]:
    """The contract's core deliverables when NOTHING was recorded against them.

    Deliberately all-or-nothing rather than per-deliverable. Matching a
    deliverable's prose against a candidate's title is a guess, and a guess
    here would either excuse an unmet promise because two sentences shared a
    word or accuse a met one because they did not. The case this CAN answer
    without guessing is the one that matters most and is unambiguous: the
    contract promised deliverables and the decision executed nothing on either
    required layer, so none of them can have been discharged.

    The phrase is not one of `REASON_PHRASES` for the same reason the
    regression phrase is not: nobody refused these, they simply did not happen.
    """
    if contract is None or not contract.core_deliverables:
        return ()
    if any(decision.by_layer(layer) for layer in REQUIRED_LAYERS):
        return ()
    count = len(contract.core_deliverables)
    return (f"{count} core deliverable(s) the contract recorded before the work "
            f"(nothing was executed on the core or professional layer)",)


#: How many items one line of the report names before it starts counting.
#:
#: Not cosmetic. The first measured turn produced a `Not done` line with
#: twenty-three entries on it, each ending in the same parenthesis, and the
#: result was unreadable -- which for a report is the same as being wrong,
#: because nobody finds the one item that mattered. The count that follows the
#: names is what keeps it honest: the reader is told how many they are not
#: seeing, and the full list is in the decision.
MAX_LISTED = 6


def _listed(items: Sequence[str]) -> str:
    """Up to `MAX_LISTED` names, then how many more there are.

    The remainder is COUNTED rather than dropped. A truncated list that ends
    without saying so is the report equivalent of a silent budget: the reader
    believes they have seen everything, and the one item that would have
    changed their mind is the twenty-third.
    """
    rows = [str(item) for item in items if str(item).strip()]
    if len(rows) <= MAX_LISTED:
        return ", ".join(rows)
    shown = ", ".join(rows[:MAX_LISTED])
    return f"{shown}, and {len(rows) - MAX_LISTED} more"


def render(closeout: Closeout) -> str:
    """§32's report, in English. One line per question a reader has.

    The Extras line is unconditional. §30's rule is that extras are never
    hidden inside the core summary, and the strongest form of hiding is not
    printing the line at all: a reader who has never seen an Extras line cannot
    tell a run that added nothing from a run that did not look. `Extras: none`
    is a positive statement and it costs one line.

    `Not consulted` is conditional, and that asymmetry is deliberate rather
    than an oversight. An empty extras list is a FINDING -- we looked, there
    were none. An empty degraded list is the absence of a caveat, and printing
    "Not consulted: none" would put a warning-shaped line on every healthy run
    until people stopped reading it.
    """
    lines = [
        f"Core: {_listed(closeout.core) or 'nothing was recorded as executed'}.",
        f"Extras: {_listed(closeout.extras) or 'none'}.",
        f"Not done: {_listed(closeout.not_done) or 'nothing was refused or deferred'}.",
        f"Proof: {closeout.proof}.",
        f"Stop reason: {closeout.stop}.",
    ]
    dark = closeout.decision.degraded_integrations
    if dark:
        it, saw = ("them", "they") if len(dark) > 1 else ("it", "it")
        lines.append(
            f"Not consulted: {', '.join(dark)} — this close was written without "
            f"{it} and does not claim what {saw} would have seen.")
    return "\n".join(lines)


def _proof_head(text: str) -> str:
    """The verdict at the head of a proof line, without the doubt behind it.

    `_proof_text` puts the verdict first and separates the doubt with an em
    dash, so the head is the verdict every time; "not established" keeps its
    two words and loses the explanation, which is what a one-line summary
    wants. Split on the em dash and then on the colon, in that order: "not
    established: ..." has a colon and no dash, the graded verdicts have a dash
    and their detail may contain colons of its own.
    """
    head = str(text or "").split(" — ", 1)[0]
    return head.split(":", 1)[0].strip() or "not established"


def summary_line(closeout: Closeout) -> str:
    """One line for a log, an event payload or a list of past runs.

    Counts and not titles: a summary that carried the first title would make
    two very different runs look alike whenever they happened to start with the
    same piece of work. The stop reason is the raw `STOP_REASONS` token here
    rather than its phrase, because this line is grepped and the phrase is
    read.
    """
    decision = closeout.decision
    parts = [
        f"{decision.mode}",
        f"{len(closeout.core)} core",
        f"{len(closeout.extras)} extra(s)",
        f"{len(closeout.not_done)} not done",
        f"proof {_proof_head(closeout.proof)}",
        f"stop {decision.stop_reason}",
    ]
    if decision.degraded_integrations:
        parts.append(f"degraded {'+'.join(decision.degraded_integrations)}")
    if decision.shadow:
        parts.append("shadow")
    return "; ".join(parts)
