"""What one improvement is worth, and what a score is NOT allowed to decide.

Section 8 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`.  The
formula the plan gives is the shape of `utility()`:

    utility = expected_value x confidence x relevance x reversibility_factor
            - cost_penalty - risk_penalty - scope_distance_penalty

Four rules are mechanised here, each with the failure it prevents:

1.  **A blocking decision is never reduced to a score.**  §8 says it in one
    line -- "no reducir decisiones blocking a un score" -- and the way to obey
    it is not to be careful when scoring, it is to not score the thing at all.
    `completion_engine/scope.py::admits` decides whether a candidate may run;
    this module decides only how good the ones that MAY run are.  Scoring
    something inadmissible would hand it an appeal: a `0.99` beside a
    `no_permission` invites exactly one question -- "can we make an exception
    for this one?" -- and the number is the single part of a candidate that
    nothing outside the engine can check.  `frontier.build` calls `admits`
    first and never scores what it refused, and there is a test that reads the
    syntax tree rather than trusting this paragraph.

2.  **`relevance` is read off the structural RELATION, never off an opinion.**
    `relation_rank` from the contract orders `direct` > `adjacent` >
    `downstream` > `similar_case` > `opportunistic` > `unrelated`, and
    `scope.relation_of` derives that relation from directories and filename
    conventions.  A relevance a model could assert would let a run justify any
    detour by describing it warmly.

3.  **`confidence` MULTIPLIES.**  A large value held with little confidence has
    to fall, not to average out to something middling.  Added, `0.9` value with
    `0.1` confidence outranks `0.5` held firmly; multiplied it does not, which
    is the ordering anybody would choose if asked, and the one arithmetic gets
    wrong by default.  `relevance` and `reversibility_factor` multiply for the
    same reason: they are all statements about whether the value is REAL, and
    a doubt about that is not repaid by a bigger number beside it.

4.  **The source is part of the score.**  `DISCOVERY_SOURCES` is ordered by
    determinism, and its comment says the order is the trust order: "a
    candidate a static check produced is a fact about the code, and one a model
    proposed is a suggestion, and the scoring reads this field rather than
    treating them alike."  This is that reading.  `SOURCE_FACTOR` never reaches
    zero -- a model's suggestion is discounted, not silenced, because silencing
    it here would hide it from the closeout as well.

`explain_score` exists because §23 keeps the terms and not the sentence: "la
explicación del score puede resumirse, pero valor/coste/riesgo/origen y
decisión son duraderos".  A single float is not auditable -- two candidates
scoring 0.41 for opposite reasons are indistinguishable in the record -- so
every term is returned separately and the total is derived from them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from src.agent_profiles.completion import policy_for
from src.completion_engine.contracts import (
    DISCOVERY_SOURCES,
    LAYERS,
    CompletionError,
    RELATIONS,
    REVERSIBILITY,
    ImprovementCandidate,
    ScopeEnvelope,
    relation_rank,
)

__all__ = [
    "WEIGHTS",
    "RELEVANCE",
    "REVERSIBILITY_FACTOR",
    "SOURCE_FACTOR",
    "utility",
    "explain_score",
    "marginal_value",
    "clears_bar",
]


#: The three penalties of §8's formula, and the one dial on scarcity.
#:
#: * `cost` (0.35) is the largest because it is the term the frontier exists to
#:   respect: a greedy engine with a small cost weight is a greedy engine that
#:   runs everything and calls it selection.
#: * `risk` (0.30) sits just below it.  It is deliberately NOT larger, because
#:   risk's real job is done elsewhere: a blocking risk is refused at admission
#:   by `frontier`, whatever the value, and this weight only separates the
#:   merely-uncomfortable from the cheap and safe.  A weight large enough to
#:   veto would be a second, softer veto competing with the real one.
#: * `scope_distance` (0.20) prices the detour §12 warns about.  Distance is
#:   already multiplied in through `relevance`; this term is the extra push
#:   that keeps a very valuable `similar_case` from outranking a middling
#:   `direct` one, because finishing the errand beats finding a better one.
#: * `scarcity` (0.15) scales the cost penalty by how full the turn already is,
#:   and it is the term most easily mistaken for its opposite.  It reads the
#:   share of the budget SPENT SO FAR overall -- never what was spent on this
#:   candidate -- so it says "room is running out, be pickier", which is
#:   forward-looking.  §9's "coste hundido no justifica continuar" forbids the
#:   other direction, and the other direction would need a number this module
#:   is never given: `utility` has no way to ask what any candidate has cost.
WEIGHTS: Dict[str, float] = {
    "cost": 0.35,
    "risk": 0.30,
    "scope_distance": 0.20,
    "scarcity": 0.15,
}

#: §6's relations turned into a multiplier, on rule 2's structural ordering.
#: `unrelated` is 0.0 rather than a small number: it is the value
#: `scope.relation_of` returns for a PROTECTED resource, and a protected
#: resource must not be able to earn its way back with a high expected value.
RELEVANCE: Dict[str, float] = {
    "direct": 1.0,
    "adjacent": 0.8,
    "downstream": 0.65,
    "similar_case": 0.5,
    "opportunistic": 0.25,
    "unrelated": 0.0,
}

#: §12 allows acting unasked only when the change is reversible, so
#: irreversibility discounts the value rather than adding a penalty to it: an
#: irreversible improvement is not a good one with a caveat, it is a smaller
#: bet.  `none` keeps 0.3 instead of 0.0 because irreversible work is still
#: sometimes the errand itself -- a deploy that was literally requested -- and
#: zero here would make `core` unscoreable.
REVERSIBILITY_FACTOR: Dict[str, float] = {"full": 1.0, "partial": 0.6, "none": 0.3}

#: Rule 4: `DISCOVERY_SOURCES` in its own order, read as trust.  The scale is
#: 1.0 for a source that OBSERVED the gap down to 0.55 for one that inferred
#: it.  The deterministic sources -- a definition-of-done line nobody ticked, a
#: static finding, a failing test, a delta, a proof that came back short -- are
#: facts about the artefact, and they keep 1.0.  The assisted ones taper: a
#: reviewer or a council read something real, a model on its own did not.
#: `user` is 1.0 because a person asking mid-run is not evidence to be
#: discounted, it is the request being amended.
SOURCE_FACTOR: Dict[str, float] = {
    "definition_of_done": 1.0,
    "static_check": 1.0,
    "tests": 1.0,
    "delta": 0.95,
    "proof": 0.95,
    "state_mirror": 0.9,
    "changeset": 0.9,
    "playbook": 0.85,
    "similar_case": 0.8,
    "opportunity": 0.75,
    "reviewer": 0.7,
    "council": 0.7,
    "model": 0.55,
    "user": 1.0,
}

#: A source this build does not know is treated as the least deterministic one
#: it does know.  Ranking an unrecognised word ABOVE `model` would let a typo
#: in a generator promote its own suggestions.
_UNKNOWN_SOURCE_FACTOR = 0.55


# Every closed vocabulary this module turns into a number is checked at import
# against the contract that owns it.  The failure being prevented is silent and
# slow: a relation, a reversibility or a discovery source added to
# `contracts.py` and not to the table beside it would score as the default --
# `unrelated` at 0.0, `none` at 0.3, `model` at 0.55 -- so a NEW and better
# source of candidates would arrive already discounted, and nothing would say
# so.  Import time is the right place because a scoring table is data, and
# checking data is cheap exactly once.
_gaps = tuple(
    "{}:{}".format(vocab, name)
    for vocab, names, table in (
        ("relation", RELATIONS, RELEVANCE),
        ("reversibility", REVERSIBILITY, REVERSIBILITY_FACTOR),
        ("source", DISCOVERY_SOURCES, SOURCE_FACTOR),
    )
    for name in names if name not in table
)
if _gaps:  # pragma: no cover - a programming error, not input
    raise CompletionError(
        "scoring tables",
        "do not cover every value the contract allows: {}; an unscored value "
        "silently takes the least favourable default, so a newly added and "
        "MORE deterministic source would arrive discounted".format(list(_gaps)),
        got=list(_gaps),
    )
del _gaps


def _relation_of(candidate: ImprovementCandidate, relation: str) -> str:
    """The relation to score with: the caller's, else the candidate's own.

    The caller's wins because it is the one `scope.relation_of` computed from
    paths.  A candidate carries a `relation` field that some generator set, and
    rule 2 is that structure outranks assertion whenever both are available.
    """
    name = str(relation or "") or str(candidate.relation or "")
    return name if name in RELATIONS else "unrelated"


def _scope_distance(relation: str, envelope: Optional[ScopeEnvelope]) -> float:
    """0.0 at `direct`, 1.0 at `unrelated`, by position in `RELATIONS`.

    When an envelope is supplied, a relation it does not permit is scored at
    full distance whatever its rank.  That is not the refusal -- `admits` has
    already made it, or will -- it is the arithmetic agreeing with the refusal
    so that a caller who scores first and filters later cannot produce a
    flattering number for something the envelope excludes.
    """
    rank = relation_rank(relation)
    distance = rank / float(max(1, len(RELATIONS) - 1))
    if envelope is not None and not envelope.permits_relation(relation):
        return 1.0
    return round(distance, 4)


def explain_score(candidate: ImprovementCandidate, *, relation: str = "",
                  envelope: Optional[ScopeEnvelope] = None,
                  spent_share: float = 0.0) -> Dict[str, Any]:
    """Every term of §8's formula, separately, plus the total they produce.

    §23 is the reason this is the primary function and `utility()` the
    convenience wrapper around it: "valor/coste/riesgo/origen y decisión son
    duraderos".  A closeout that stored only the float could not answer why a
    candidate lost -- a 0.41 from a high value heavily discounted for risk and
    a 0.41 from a modest value with none look identical -- and "why did it skip
    that?" is the question this engine exists to be able to answer.

    The returned mapping is JSON-safe and its `terms` are the multiplicands and
    the penalties under their own names, so `utility` is reproducible from the
    record without re-reading the candidate.
    """
    name = _relation_of(candidate, relation)
    relevance = RELEVANCE.get(name, 0.0)
    reversibility = REVERSIBILITY_FACTOR.get(str(candidate.reversibility or ""), 0.3)
    source = SOURCE_FACTOR.get(str(candidate.source or ""), _UNKNOWN_SOURCE_FACTOR)
    distance = _scope_distance(name, envelope)

    share = min(1.0, max(0.0, float(spent_share or 0.0)))
    # Rule 3, and the arithmetic that makes it true: value, confidence,
    # relevance, reversibility and source all MULTIPLY.  Every one of them is a
    # statement about whether the value is real, and doubts about that compound
    # rather than cancel.
    gross = (float(candidate.expected_value)
             * float(candidate.confidence)
             * relevance
             * reversibility
             * source)
    cost_penalty = WEIGHTS["cost"] * float(candidate.estimated_cost) * (
        1.0 + WEIGHTS["scarcity"] * share)
    risk_penalty = WEIGHTS["risk"] * float(candidate.risk)
    scope_penalty = WEIGHTS["scope_distance"] * distance
    total = gross - cost_penalty - risk_penalty - scope_penalty
    return {
        "id": candidate.id,
        "title": candidate.title,
        "layer": candidate.layer,
        "relation": name,
        "source": candidate.source,
        "terms": {
            "expected_value": round(float(candidate.expected_value), 4),
            "confidence": round(float(candidate.confidence), 4),
            "relevance": round(relevance, 4),
            "reversibility_factor": round(reversibility, 4),
            "source_factor": round(source, 4),
            "gross": round(gross, 4),
            "cost_penalty": round(cost_penalty, 4),
            "risk_penalty": round(risk_penalty, 4),
            "scope_distance_penalty": round(scope_penalty, 4),
            "scope_distance": distance,
            "spent_share": round(share, 4),
        },
        "utility": round(total, 4),
    }


def utility(candidate: ImprovementCandidate, *, relation: str = "",
            envelope: Optional[ScopeEnvelope] = None,
            spent_share: float = 0.0) -> float:
    """§8's score for a candidate that has ALREADY been admitted.  Rule 1.

    This function does not decide anything blocking and must never be asked to.
    Permission, forbidden effects, protected resources, working area and
    relation are `scope.admits`'s answer, and a blocking risk is `frontier`'s;
    calling `utility` on something none of them admitted produces a number that
    reads like a case for an exception.  §8: "un candidato fuera de Scope
    Envelope o sin permiso queda excluido" -- excluded, not scored low.

    `spent_share` is the share of the turn's budget already gone, across the
    run.  It makes the engine pickier as room runs out, which is the marginal
    reading; it is NOT what this candidate has cost so far, and no argument
    here carries that.  §9's rule is that sunk cost never justifies continuing,
    and the way this module obeys it is by having no way to know.

    The result is unclamped, and negative is meaningful: it says the penalties
    outweighed the value, which is precisely the candidate a `minimum` of 0.0
    is meant to drop.  Clamping to zero would make "barely worth it" and
    "actively not worth it" the same number.
    """
    return float(explain_score(candidate, relation=relation, envelope=envelope,
                               spent_share=spent_share)["utility"])


def _overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    return bool({str(x) for x in a} & {str(y) for y in b})


def marginal_value(candidate: ImprovementCandidate, *,
                   executed: Sequence[ImprovementCandidate] = ()) -> float:
    """What this candidate is still worth GIVEN what has already run.  §9.

    "Después de cada mejora: actualizar valor esperado de restantes."  The
    update is a discount and never a credit: work already done can make a
    remaining candidate redundant, and nothing it does can make one worth more.
    An engine that could raise a score after spending on something else would
    be able to talk itself into a second round of the same detour.

    Three degrees, from the strongest structural evidence to the weakest:

    * the same `key` has run -- the contract's own dedupe key -- so this is the
      same improvement and is worth nothing;
    * this candidate DEPENDS on nothing that ran but shares its resources and
      category, so the same pass over the same files probably covered it;
    * it merely shares resources, so it is somewhat cheaper to doubt.

    Sunk cost cannot enter here either: `executed` is read for what it COVERED,
    never for what it cost.  There is no cost field consulted in this function
    and a test asserts that by reading the syntax tree.
    """
    value = float(candidate.expected_value)
    if value <= 0.0:
        return 0.0
    done = tuple(executed or ())
    for other in done:
        if other.id == candidate.id or other.key == candidate.key:
            return 0.0
    factor = 1.0
    for other in done:
        if not _overlap(candidate.resources, other.resources):
            continue
        factor *= 0.5 if other.category == candidate.category else 0.8
    return round(max(0.0, value * factor), 4)


def clears_bar(candidate: ImprovementCandidate, *, mode: str,
               minimum: float = 0.0, **kw: Any) -> Tuple[bool, str]:
    """`(ok, reason)`: is this worth running, under this mode's depth?

    The reason is a `REJECTION_REASONS` value so it can be stored on the
    candidate without translation, and the two it can return say different
    things on purpose:

    * `out_of_scope` -- the MODE never opens this candidate's layer.  That is
      not a judgement about the candidate at all: a `literal` run refusing a
      `bonus` improvement has said nothing about whether the improvement was
      good, and recording it as `below_threshold` would put a value judgement
      in the record that nobody made.  §4 sets the depth with
      `max_extra_layers`, and `CompletionContract.layers()` derives the allowed
      layers from it; the same derivation is used here rather than a second
      table that could drift from it.
    * `below_threshold` -- the layer was open and the utility did not clear
      `minimum`.  §9: "detenerse cuando ninguna supera threshold".

    `**kw` is forwarded to `utility`, so `relation`, `envelope` and
    `spent_share` all reach the score.  It carries no bar of its own: a caller
    cannot pass something that raises or lowers the threshold sideways, because
    the only threshold is `minimum` and it is a named argument.
    """
    policy = policy_for(mode)
    allowed = LAYERS[:min(1 + max(0, int(policy.max_extra_layers)), len(LAYERS))]
    if candidate.layer not in allowed:
        return False, "out_of_scope"
    score = utility(candidate, **kw)
    if score < float(minimum):
        return False, "below_threshold"
    return True, ""
