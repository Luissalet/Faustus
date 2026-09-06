"""The value frontier: who is admitted, who is worth it, and what runs together.

Sections 8, 9 and 10 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`.
`scope.py` decides whether a candidate MAY run and `scoring.py` decides how
good the ones that may are; this module is the pipeline that puts them in the
one order that is safe, and keeps the receipts.

**The order, and why it is the whole design:**

    dedupe -> admission -> scoring -> Pareto -> budget -> batch

Admission comes first and nothing that fails it is ever scored.  §8: "no
reducir decisiones blocking a un score.  Un candidato fuera de Scope Envelope
o sin permiso queda excluido" -- excluded, not scored low.  The difference is
not stylistic.  A refused candidate carrying a 0.94 beside `no_permission` is
an invitation to make an exception, and the number is the one part of a
candidate that nothing outside this engine can verify.  `_admit` therefore
cannot read `expected_value`, `confidence` or `estimated_cost` at all, and
`tests/test_completion_engine_frontier.py` asserts that by parsing this
module's own syntax tree -- the same technique `scope.py` uses for the same
guarantee, and for the same reason: behaviour can be correct today and one new
line can make it wrong tomorrow without any test noticing.

**A blocking risk is admission, not arithmetic.**  §27 asks that a blocking
risk exclude a candidate, and the test that fixes it uses `expected_value =
1.0`.  If risk were only a penalty term, a large enough value would always
outrun it; so `BLOCKING_RISK` is checked in `_admit`, before any score exists,
and the rejection reason is `risk`.  `scoring.WEIGHTS["risk"]` still prices the
non-blocking remainder, and is deliberately too small to veto anything, so
there is exactly one veto rather than two that could disagree.

**Batching keeps its origins.**  §10 ends with the requirement that survives
every optimisation: "el batching reduce coste, pero no debe ocultar qué mejora
originó cada cambio."  `batch()` returns the candidates themselves rather than
a merged unit of work, so every id is still there to be attributed, and
`Frontier.to_dict` records the batch by id beside the entries it came from.

**Every rejection carries a reason from `REJECTION_REASONS`.**  §1.8's rule is
that a rejected opportunity must not reappear without new evidence, and a
rejection nobody recorded reappears every round.  `FrontierEntry.reason` is
that record and `Frontier.rejected()` is how the closeout reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.completion_engine import budgeting, scoring
from src.completion_engine.contracts import (
    REJECTION_REASONS,
    SPENDABLE_UNITS,
    CompletionBudget,
    ImprovementCandidate,
    ScopeEnvelope,
    relation_rank,
)
from src.completion_engine.scope import admits

__all__ = [
    "BLOCKING_RISK",
    "FrontierEntry",
    "Frontier",
    "build",
    "pareto",
    "batch",
    "recompute",
]


#: Above this, a candidate is refused whatever it is worth.  §27's own test
#: pairs a blocking risk with `expected_value = 1.0`, which is the case a
#: penalty term cannot handle: any weight small enough to leave ordinary risky
#: work scoreable is a weight a 1.0 will outrun.  A threshold does not have
#: that property, and 0.8 is where "this could break something we did not come
#: here to touch" starts.
#:
#: Deliberately NOT read from the mode.  Depth is not authority (§3.3), and a
#: `maximalist` allowed to accept riskier work than a `literal` would be a mode
#: granting something, which is the one thing a mode may never do.
BLOCKING_RISK = 0.8

#: How close two risks must be to travel in the same batch.  §10 forbids
#: grouping "riesgo/permisos distintos", and without a band that rule is either
#: exact-equality -- which never groups anything, since risks are floats -- or
#: nothing.  A band is the honest middle: 0.2 keeps "both effectively safe"
#: together and separates it from "one of these needs watching".
_RISK_BAND = 0.2


@dataclass(frozen=True)
class FrontierEntry:
    """One candidate, its verdict, and the terms that produced it.

    `terms` is `scoring.explain_score`'s breakdown and is EMPTY for a candidate
    that never reached scoring.  That emptiness is the record of the order: an
    entry refused at admission has no score because none was computed, not
    because the score was hidden, and §23's rule that origin and decision are
    durable is served by being able to tell those two apart afterwards.
    """

    candidate: ImprovementCandidate
    utility: float = 0.0
    relation: str = ""
    admitted: bool = False
    reason: str = ""
    terms: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.terms is None:
            object.__setattr__(self, "terms", {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "id": self.candidate.id,
            "utility": self.utility,
            "relation": self.relation,
            "admitted": self.admitted,
            "reason": self.reason,
            "terms": dict(self.terms),
        }


@dataclass(frozen=True)
class Frontier:
    """The ordered verdict on one round's candidates.

    `entries` holds every candidate that was considered, admitted or not, in
    the order the engine would run them.  Keeping the refusals in the same
    structure as the selections is what makes §1.8 enforceable: the next round
    can see that something was already refused, and why, without a second store
    to consult.

    `batched` and `batch_reason` are left EMPTY by `build`, and that is not an
    oversight.  Batching is a later and separate decision -- §10 groups what
    the frontier already selected -- and a `build` that also batched would make
    the two indistinguishable in the record: a reader could no longer tell a
    candidate the frontier rejected from one that was admitted and simply did
    not travel in this round's lot.  A caller that has run `batch()` records
    its result here, beside the entries it came from, which is what keeps the
    §10 requirement -- that a lot never hides which improvement caused what --
    true of the persisted object and not only of the return value.
    """

    entries: Tuple[FrontierEntry, ...] = ()
    generated_at: str = ""
    batched: Tuple[str, ...] = ()
    batch_reason: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            object.__setattr__(self, "generated_at", now_iso())

    def selected(self) -> Tuple[ImprovementCandidate, ...]:
        return tuple(e.candidate for e in self.entries if e.admitted)

    def rejected(self) -> Tuple[ImprovementCandidate, ...]:
        """The refused candidates, each carrying its reason ON the candidate.

        The reason is written into the candidate rather than left only on the
        entry because a candidate outlives this object: it goes into
        `CompletionDecision.rejected`, which parses it and refuses a `rejected`
        status with no reason.  Handing back the original would produce a
        decision the contract will not accept, at the end of the run, where the
        failure is most expensive.
        """
        out = []
        for entry in self.entries:
            if entry.admitted:
                continue
            reason = entry.reason if entry.reason in REJECTION_REASONS else "below_threshold"
            payload = entry.candidate.to_dict()
            payload["status"] = "rejected"
            payload["rejection_reason"] = reason
            out.append(ImprovementCandidate.parse(payload, "candidate"))
        return tuple(out)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "entries": [e.to_dict() for e in self.entries],
            "selected": [c.id for c in self.selected()],
            "rejected": [{"id": c.id, "reason": c.rejection_reason}
                         for c in self.rejected()],
            "batched": list(self.batched),
            "batch_reason": self.batch_reason,
        }


# -- step 1: dedupe --------------------------------------------------------


def _dedupe(candidates: Sequence[ImprovementCandidate]
            ) -> Tuple[Tuple[ImprovementCandidate, ...],
                       Tuple[ImprovementCandidate, ...]]:
    """`(kept, duplicates)` by `ImprovementCandidate.key`.

    First occurrence wins rather than best-scoring, because at this point in
    the pipeline no score exists -- choosing by value here would mean scoring
    before admission, which is exactly the order this module refuses.  §7's
    sources are ordered by determinism and callers are expected to offer them
    in that order, so first-wins keeps the more trustworthy generator's phrasing
    of the same finding.
    """
    seen: Dict[str, ImprovementCandidate] = {}
    kept = []
    dupes = []
    for candidate in candidates or ():
        key = candidate.key
        if key in seen:
            dupes.append(candidate)
            continue
        seen[key] = candidate
        kept.append(candidate)
    return tuple(kept), tuple(dupes)


# -- step 2: admission -----------------------------------------------------


def _admit(candidate: ImprovementCandidate, envelope: ScopeEnvelope, *,
           permissions: Any = None,
           resolved: Sequence[str] = (),
           declined: Sequence[str] = (),
           executed_ids: Sequence[str] = ()) -> Tuple[bool, str]:
    """`(ok, reason)`.  Blocking decisions only -- never a value.

    The gates, in order, and each answers with a `REJECTION_REASONS` value:

        already executed                                  -> `duplicate`
        a person said no to it                            -> `declined`
        resolved on the way past                          -> `resolved`
        permission / effect / protected / area / relation -> `scope.admits`
        blocking risk                                     -> `risk`
        unmet dependency                                  -> `quarantined`

    **This function may not read `expected_value`, `confidence` or
    `estimated_cost`, and a test parses it to prove that.**  It reads `risk`,
    which is the deliberate exception: §8 forbids reducing a BLOCKING decision
    to a score, and a blocking risk is a blocking decision -- so risk is read
    here as a threshold and not as a term.  Value is what must not appear,
    because value is the axis on which an exception gets argued.

    An unmet dependency is `quarantined` rather than `blocked`: `blocked` is a
    STOP reason for the whole run, and `REJECTION_REASONS` has no entry for
    "not yet".  `quarantined` -- "depends on a capability that is not healthy"
    -- is the closest true statement, and it keeps the candidate eligible next
    round once the dependency lands, which `rejected` forever would not.
    """
    if candidate.id in set(executed_ids or ()):
        return False, "duplicate"
    # Second, and above every judgement this function is otherwise allowed to
    # make: a person already answered this question.  §1.8 -- a rejected
    # opportunity must not reappear without new evidence -- and the gate sits
    # here, in admission, for the same reason every other blocking gate does:
    # below this line candidates acquire a utility, and a refusal that had to
    # outrank a high value would be a refusal something could argue with.
    #
    # Matched on `key` and not `id`.  The id is minted per decision; the same
    # improvement rediscovered tomorrow carries a new one, and an id match
    # would enforce the refusal exactly once -- against the row the person was
    # looking at, and never against the thing they were refusing.
    refused = set(declined or ())
    if refused and (candidate.key in refused
                    or (candidate.dedupe_key and candidate.dedupe_key in refused)):
        return False, "declined"
    marks = set(resolved or ())
    if candidate.id in marks or (candidate.dedupe_key and candidate.dedupe_key in marks):
        return False, "resolved"
    ok, reason = admits(candidate, envelope, permissions=permissions)
    if not ok:
        return False, reason
    if float(candidate.risk) >= BLOCKING_RISK:
        return False, "risk"
    pending = [d for d in candidate.dependencies
               if d not in set(executed_ids or ()) and d not in marks]
    if pending:
        return False, "quarantined"
    return True, ""


# -- step 4: the frontier proper -------------------------------------------


def _dominates(a: FrontierEntry, b: FrontierEntry) -> bool:
    """Whether `a` beats `b` on value, cost and risk with nothing given up.

    Pareto on the three axes §8 names, and the comparison is on the RAW
    candidate fields rather than on `utility`: a single score has already
    collapsed the three into one number, and a dominance test run on the
    collapsed number would only ever say "higher score wins", which is sorting,
    not domination.  The point of keeping the frontier is that a cheap, safe,
    modest improvement is NOT dominated by an expensive, risky, brilliant one.
    """
    av, bv = float(a.candidate.expected_value), float(b.candidate.expected_value)
    ac, bc = float(a.candidate.estimated_cost), float(b.candidate.estimated_cost)
    ar, br = float(a.candidate.risk), float(b.candidate.risk)
    at_least = av >= bv and ac <= bc and ar <= br
    strictly = av > bv or ac < bc or ar < br
    return at_least and strictly


def pareto(entries: Sequence[FrontierEntry]) -> Tuple[FrontierEntry, ...]:
    """The non-dominated entries, in the order they were given.

    §8: "mantener candidatas no dominadas por valor/coste/riesgo".  Only
    admitted entries take part -- a refused candidate has no place on a value
    frontier, and letting one dominate an admitted one would silently
    substitute a permission failure for a value judgement in the record.

    Two identical candidates do not dominate each other (`strictly` is False
    for a tie on all three axes), so a tie keeps both.  Dropping one would make
    the survivor depend on dictionary order.
    """
    live = [e for e in entries if e.admitted]
    out = []
    for entry in live:
        if any(_dominates(other, entry) for other in live if other is not entry):
            continue
        out.append(entry)
    return tuple(out)


# -- the pipeline ----------------------------------------------------------


def _spent_share(budget: Optional[CompletionBudget]) -> float:
    """How full the turn is, as the worst-off unit.  Feeds `scoring`'s scarcity.

    The MAXIMUM across units and not the mean: a run with tokens to spare and
    no rounds left is out of room, and averaging would report it half free
    right up to the moment it stopped.  Units the budget does not count are
    skipped rather than counted as empty.
    """
    if budget is None:
        return 0.0
    shares = []
    for pot in budgeting.FUNDED_LINES:
        for unit in SPENDABLE_UNITS:
            ceiling = budget.ceiling(pot, unit)
            if ceiling <= 0:
                continue
            shares.append(min(1.0, budget.used(pot, unit) / ceiling))
    return round(max(shares), 4) if shares else 0.0


def build(candidates: Sequence[ImprovementCandidate], *,
          envelope: ScopeEnvelope,
          budget: Optional[CompletionBudget] = None,
          mode: str = "greedy",
          permissions: Any = None,
          executed: Sequence[ImprovementCandidate] = (),
          resolved: Sequence[str] = (),
          declined: Sequence[str] = (),
          minimum: float = 0.0,
          line: str = "bonus") -> Frontier:
    """One round's frontier, in the one order that is safe.

        dedupe -> admission -> scoring -> Pareto -> budget

    Read the body as the proof of the order: `_admit` is called for every
    candidate and its verdict recorded BEFORE the loop that scores anything,
    and the scoring loop iterates only over what admission passed.  Nothing
    refused ever acquires a utility -- §8's "queda excluido", mechanised rather
    than promised -- and the test suite checks it three ways: `_admit`'s syntax
    tree contains no value field, `build`'s source calls `_admit` above its
    first scoring call, and a refused entry comes back with `terms == {}`.

    `budget` is optional because the frontier is meaningful without one -- a
    caller that only wants to know what is admissible and what it is worth
    passes none, and no candidate is refused for `budget` in that case.  Given
    one, affordability is asked per candidate against `line` through
    `budgeting.afford`, which resolves the line to the pot that funds it.

    `minimum` is the marginal bar of §9.  A candidate that does not clear it is
    refused with `below_threshold`, which is a value judgement and is recorded
    as one -- as against `out_of_scope`, which `clears_bar` returns when the
    MODE never opens the candidate's layer and no judgement about the candidate
    was made at all.
    """
    kept, dupes = _dedupe(candidates)
    executed_ids = tuple(c.id for c in executed or ())

    # -- admission.  Blocking only; no score exists yet, by construction.
    verdicts: list = []
    for candidate in kept:
        ok, reason = _admit(candidate, envelope, permissions=permissions,
                            resolved=resolved, declined=declined,
                            executed_ids=executed_ids)
        verdicts.append((candidate, ok, reason))

    # -- scoring.  Only over what admission passed.
    share = _spent_share(budget)
    entries: list = []
    for candidate, ok, reason in verdicts:
        if not ok:
            entries.append(FrontierEntry(candidate=candidate, utility=0.0,
                                         relation=candidate.relation,
                                         admitted=False, reason=reason, terms={}))
            continue
        detail = scoring.explain_score(candidate, envelope=envelope,
                                       spent_share=share)
        cleared, why = scoring.clears_bar(candidate, mode=mode, minimum=minimum,
                                          envelope=envelope, spent_share=share)
        entries.append(FrontierEntry(
            candidate=candidate,
            utility=float(detail["utility"]),
            relation=str(detail["relation"]),
            admitted=bool(cleared),
            reason="" if cleared else why,
            terms=dict(detail["terms"]),
        ))

    for candidate in dupes:
        entries.append(FrontierEntry(candidate=candidate, utility=0.0,
                                     relation=candidate.relation,
                                     admitted=False, reason="duplicate", terms={}))

    # -- Pareto.  Dominated candidates lose on value/cost/risk, not on score.
    survivors = {id(e) for e in pareto(entries)}
    entries = [e if (not e.admitted or id(e) in survivors)
               else FrontierEntry(candidate=e.candidate, utility=e.utility,
                                  relation=e.relation, admitted=False,
                                  reason="dominated", terms=e.terms)
               for e in entries]

    # -- budget.  Last, so that "we could not afford it" is never confused
    #    with "it was not worth it": the two lead to opposite next actions.
    if budget is not None:
        priced: list = []
        for entry in entries:
            if not entry.admitted:
                priced.append(entry)
                continue
            ok, why = budgeting.afford(budget, line, entry.candidate)
            priced.append(entry if ok else FrontierEntry(
                candidate=entry.candidate, utility=entry.utility,
                relation=entry.relation, admitted=False, reason=why,
                terms=entry.terms))
        entries = priced

    entries.sort(key=lambda e: (not e.admitted,
                                relation_rank(e.relation),
                                -e.utility))
    return Frontier(entries=tuple(entries), generated_at=now_iso())


# -- step 6: batching ------------------------------------------------------


def _risk_band(value: float) -> int:
    return int(float(value) / _RISK_BAND)


def _batch_key(candidate: ImprovementCandidate) -> Tuple[Any, ...]:
    """What makes two candidates safe to run together.  §10's two lists.

    Grouped by: the component they touch, the permissions they need, the
    effects they cause, how reversible they are, the risk band, and the
    verification that would prove them.  Every one of those is on §10's
    "no agrupar" list when it differs -- "riesgo/permisos distintos; efectos
    externos independientes; ... candidatas competidoras" -- so equality on all
    of them is what "compatible" means, and the key is the enforcement.

    Permissions and effects are in the key rather than checked afterwards
    because that is the difference between a rule and a reminder: two
    candidates needing different authority cannot land in the same group at
    all, so no later code has to remember to split them.

    The component is the sorted resource set.  Same files is §10's first
    grouping criterion, and it is also the one that makes a shared
    verification pass honest: one test run proves two changes only if they are
    changes to the same thing.
    """
    return (
        tuple(sorted(candidate.resources)),
        tuple(sorted(candidate.required_permissions)),
        tuple(sorted(candidate.required_effects)),
        candidate.reversibility,
        _risk_band(candidate.risk),
        tuple(sorted(candidate.verification)),
    )


def batch(entries: Sequence[FrontierEntry], *,
          budget: Optional[CompletionBudget] = None,
          line: str = "bonus") -> Tuple[Tuple[ImprovementCandidate, ...], str]:
    """`(candidates, reason)`: the best compatible group that fits.  §10.

    The candidates come back AS CANDIDATES, each with its own id, and that is
    the requirement §10 ends on: "el batching reduce coste, pero no debe
    ocultar qué mejora originó cada cambio."  A batch that returned one merged
    unit of work would save exactly the same tokens and lose the attribution
    permanently -- and the attribution is the thing that lets a reader ask
    "which improvement caused this change?" six months later.

    Groups never mix permissions, effects, risk bands or reversibility; see
    `_batch_key`.  The group with the highest total utility wins, and members
    are added in utility order while the budget affords them.

    `reason` is `""` when the whole group fits and `"budget"` when it did not,
    so a caller can tell "this is the batch" from "this is as much of the batch
    as there was room for" -- which are the same tuple and different facts.
    An empty group returns `("", ())`-shaped emptiness with reason `""`: there
    was nothing to batch, which is not a budget failure.
    """
    live = [e for e in entries if e.admitted]
    if not live:
        return (), ""
    groups: Dict[Tuple[Any, ...], list] = {}
    for entry in live:
        groups.setdefault(_batch_key(entry.candidate), []).append(entry)
    best = max(groups.values(), key=lambda g: (sum(e.utility for e in g), len(g)))
    best = sorted(best, key=lambda e: -e.utility)
    if budget is None:
        return tuple(e.candidate for e in best), ""
    chosen: list = []
    reason = ""
    running = budget
    for entry in best:
        ok, why = budgeting.afford(running, line, entry.candidate)
        if not ok:
            reason = why
            continue
        chosen.append(entry.candidate)
        running = budgeting.record(
            running, line, budgeting.estimate(
                entry.candidate,
                unit_costs={u: budget.ceiling(budgeting.funding_line(line), u)
                            for u in SPENDABLE_UNITS}))
    return tuple(chosen), reason


# -- step 7: after the round -----------------------------------------------


def recompute(previous: Frontier, *,
              executed: Sequence[ImprovementCandidate] = (),
              resolved: Sequence[str] = (),
              envelope: Optional[ScopeEnvelope] = None,
              budget: Optional[CompletionBudget] = None,
              mode: str = "greedy",
              permissions: Any = None,
              minimum: float = 0.0,
              line: str = "bonus") -> Frontier:
    """§9's "marginalidad": the frontier again, knowing what just happened.

    Three updates, and each is one of §9's bullets:

    * "eliminar las resueltas indirectamente" -- an id or dedupe key in
      `resolved` disappears with the reason `resolved`, which is its own
      `REJECTION_REASONS` entry precisely so that "somebody else fixed it" is
      never filed as "it was not worth doing".
    * "no repetir las ya ejecutadas" -- anything in `executed` comes back as
      `duplicate`.  Both of those happen inside `_admit`, at admission, so a
      candidate that is already done is never scored again either.
    * "actualizar valor esperado de restantes" -- every survivor's
      `expected_value` is replaced by `scoring.marginal_value` against what
      ran.  The update is a DISCOUNT only; work already done can make a
      remaining candidate redundant and nothing it does can make one worth
      more.

    A candidate whose discounted value no longer clears `minimum` comes out
    with `below_threshold`, which is what makes the bar meaningful across
    rounds instead of only within one.

    The real cost of what ran reaches this through `budget`: the caller books
    it with `budgeting.record` and passes the budget back, so §27's "el coste
    real actualiza el score de las restantes" happens through the scarcity term
    rather than through an estimate this module made up.  Passing the estimate
    back would produce a run that only ever agreed with itself.

    `envelope` may be omitted only when `previous` had none to begin with;
    there is no default envelope here, because inventing one would mean
    admitting candidates against a boundary nobody set.
    """
    if envelope is None:
        raise ValueError(
            "recompute needs the run's ScopeEnvelope: re-admitting candidates "
            "without one would mean checking them against a boundary nobody "
            "set, and admission is the step that must never be skipped")
    done = tuple(executed or ())
    updated = []
    for entry in previous.entries:
        candidate = entry.candidate
        payload = candidate.to_dict()
        payload["expected_value"] = scoring.marginal_value(candidate, executed=done)
        payload.pop("rejection_reason", None)
        payload["status"] = "candidate"
        updated.append(ImprovementCandidate.parse(payload, "candidate"))
    return build(updated, envelope=envelope, budget=budget, mode=mode,
                 permissions=permissions, executed=done, resolved=resolved,
                 minimum=minimum, line=line)
