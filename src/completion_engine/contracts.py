"""Greedy Completion Engine: how far to push, and when to stop honestly.

This module holds the shapes and the closed vocabularies of the completion
policy described in `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`.
Nothing here executes anything, reads a file or calls a model: it is the
contract the scope, the frontier, the budget and the closeout all agree on.

What it is NOT is a second copy of the completion MODE. `src/agent_profiles/
completion.py` already owns `COMPLETION_MODES`, `CompletionPolicy` and
`resolve_mode`, and this package imports them rather than restating them. Plan 3
built that policy properly -- versioned, with precedence as data and an
instruction reader in two languages -- and what it never got was a consumer.
This package is the consumer. Re-declaring the modes here would produce two
vocabularies that agree until the day they do not.

The seven rules this file exists to enforce, each with the failure it prevents:

1.  **Depth is not authority.** A mode, a layer and a candidate grant nothing.
    `ImprovementCandidate` has `required_permissions` -- what it would NEED --
    and no field into which a permission could be written, and the engine
    filters a candidate through the run's `PermissionEnvelope` BEFORE scoring
    it, never after. `agent_profiles/completion.py` blinds its own policy the
    same way and there is a test that reads its field names; this file is the
    side that could have broken the guarantee sideways.

2.  **A stop for budget is never reported as convergence.** `STOP_REASONS`
    keeps them apart, and `CompletionDecision.parse` refuses `converged` while
    a budget line is exhausted. "We ran out" dressed as "there was nothing left
    worth doing" is the single most misleading sentence this engine could
    produce, because it is the one that stops anyone from raising the budget.

3.  **Core and bonus are separate, always.** Every candidate carries its
    `layer`, and the closeout reports them apart. An engine that could fold
    unrequested work into the account of what was asked for is an engine whose
    user cannot tell what they got.

4.  **A verification reserve is not spendable.** `CompletionBudget` records
    SPENDS, never remainders -- the council's `BudgetState` learned this first
    -- and `may_spend()` refuses to touch the reserve for anything but
    verification, in every mode. Depth is negotiable; evidence is not.

5.  **The intent is not rewritten to justify an extra.** Nothing here holds a
    goal that can be edited: `ScopeEnvelope` references the frozen
    `IntentContract` by id and `CompletionContract` records what was promised
    at the start. A `required` classification that cannot point at evidence is
    an opinion wearing the vocabulary of necessity.

6.  **Effects come from ONE vocabulary.** `src/tool_capabilities.py::ToolEffect`
    is the one the execution guard actually consults, so it is the one used
    here. The plan writes `local_file_change`, `run_tests`, `push`, `deploy`;
    inventing those would make a FOURTH effects vocabulary in this repository
    -- there are already three -- and none of the four would translate into the
    others without losing something.

7.  **A rejection keeps its reason.** `REJECTION_REASONS` is closed and every
    rejected candidate stores one, because §1.8's rule is that a rejected
    opportunity must not reappear without new evidence, and a rejection nobody
    recorded reappears every round.

The word `partial` is deliberately absent from every vocabulary here.
`prove.VERDICTS` and `delta_engine.ASSESSMENTS` both use it, for different
things, and a third meaning would make the three impossible to read together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.agent_profiles.completion import COMPLETION_MODES, CompletionPolicy, policy_for
from src.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    as_mapping,
    fingerprint,
    flag,
    now_iso,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
)

__all__ = [
    "SCHEMA_VERSION",
    "CompletionError",
    "COMPLETION_MODES",
    "CompletionPolicy",
    "policy_for",
    "LAYERS",
    "LAYER_ORDER",
    "REQUIRED_LAYERS",
    "RELATIONS",
    "GREEDY_RELATIONS",
    "CANDIDATE_STATUSES",
    "TERMINAL_STATUSES",
    "REJECTION_REASONS",
    "REVERSIBILITY",
    "STOP_REASONS",
    "HONEST_STOPS",
    "BUDGET_LINES",
    "FUNDING_LINES",
    "FUNDED_LINES",
    "SPENDABLE_UNITS",
    "CANDIDATE_CATEGORIES",
    "DISCOVERY_SOURCES",
    "EFFECTS",
    "ScopeEnvelope",
    "CompletionContract",
    "ImprovementCandidate",
    "CompletionBudget",
    "BudgetSpend",
    "CompletionDecision",
    "new_id",
    "layer_rank",
    "relation_rank",
    "is_effect",
]


class CompletionError(ContractError):
    """A rejection from this module. Names the field and the value, always."""


# -- the four layers -------------------------------------------------------
#
# Section 3. The layers are what make over-delivery explainable: without them
# there is a pile of changes and a claim that all of it was needed.

#: Ordered from obligatory to optional. `core` is the request taken literally
#: plus whatever is indispensable for it to be TRUE; `professional` is what a
#: professional would not ship without; `bonus` is adjacent work with a real
#: return; `exploratory` is ambition, and only `maximalist` runs it.
LAYERS: Tuple[str, ...] = ("core", "professional", "bonus", "exploratory")
LAYER_ORDER: Dict[str, int] = {name: i for i, name in enumerate(LAYERS)}

#: The layers whose failure is the run's failure. A bonus that fails is a bonus
#: that failed; a core that fails is a request that was not met, and §11's rule
#: is that these two must never be reported the same way.
REQUIRED_LAYERS: Tuple[str, ...] = ("core", "professional")

# -- distance to the goal --------------------------------------------------

#: Section 6. How far a candidate sits from what was asked. This is the axis
#: that separates "finished the job" from "wandered off", and it is a
#: STRUCTURAL judgement about paths and components -- never a model's opinion
#: about whether something felt related.
RELATIONS: Tuple[str, ...] = (
    "direct",        # necessary for, or named by, the result
    "adjacent",      # same component, artifact or flow
    "downstream",    # makes the result usable
    "similar_case",  # the same pattern elsewhere, inside the envelope
    "opportunistic", # useful, not necessary
    "unrelated",     # outside
)

#: What `greedy` will run without asking. `opportunistic` and `unrelated` are
#: out: §12 allows acting unasked only when the improvement is local,
#: reversible, in scope and verifiable, and an opportunistic change is by
#: definition none of the first.
GREEDY_RELATIONS: Tuple[str, ...] = ("direct", "adjacent", "downstream", "similar_case")

# -- candidates ------------------------------------------------------------

#: Where a candidate stands. `deferred` is distinct from `rejected`: deferred
#: means worth doing and not now, and §19 hands those to the Opportunity Engine
#: rather than throwing them away.
CANDIDATE_STATUSES: Tuple[str, ...] = (
    "candidate", "selected", "executing", "done", "rejected", "deferred", "failed",
)
TERMINAL_STATUSES: Tuple[str, ...] = ("done", "rejected", "deferred", "failed")

#: Why a candidate did not run. Closed, and stored, because §1.8's rule is that
#: a rejected opportunity must not come back without new evidence -- and a
#: rejection nobody wrote down comes back every single round.
REJECTION_REASONS: Tuple[str, ...] = (
    "out_of_scope",     # outside the envelope
    "no_permission",    # the run does not hold what it would need
    "forbidden_effect", # the effect itself is denied here
    "below_threshold",  # the marginal value did not clear the bar
    "dominated",        # another candidate is better on every axis
    "duplicate",        # already covered by something selected
    "resolved",         # something else fixed it on the way past
    "quarantined",      # depends on a capability that is not healthy
    "budget",           # would not fit
    "round_full",       # fits and is wanted; this round already had its batch
    "risk",             # blocking risk, whatever the value
    "stale",            # the evidence for it aged out and did not revalidate
    "superseded",       # the state it was about has moved
    "declined",         # a PERSON said no to this one, with a reason. §12
)

#: The one rejection reason no machine may write. Everything above is a verdict
#: this engine reached about a candidate; `declined` is a verdict a person
#: reached about the engine, and the difference matters twice. Reading, because
#: "we judged this not worth it" and "you were told not to" are different facts
#: about the same unrun improvement, and rolling them together would let the
#: engine launder a refusal it was given into a judgement it made. Writing,
#: because §1.8 says a rejected opportunity must not reappear without new
#: evidence -- and the ONLY rejection that has to survive the turn that
#: produced it is this one. The other twelve are recomputed from scratch every
#: round and should be; a person should not have to say no twice.
HUMAN_REJECTION: str = "declined"

#: How hard it would be to undo. Only `full` qualifies for §12's "act without
#: asking", so this field is load-bearing rather than descriptive.
REVERSIBILITY: Tuple[str, ...] = ("full", "partial", "none")

# -- stopping --------------------------------------------------------------

#: Section 11's exits, plus the two the plan writes in prose. Every run ends
#: with exactly one of these, and rule 2 of the module docstring is about the
#: first two: they must never be confused.
STOP_REASONS: Tuple[str, ...] = (
    "converged",       # nothing left cleared the marginal bar
    "unfinished",      # the turn ended with work this mode says was left
    "budget",          # a budget line ran out
    "scope",           # the next useful thing was outside the envelope
    "risk",            # a blocking risk stopped further work
    "blocked",         # something outside this run has to happen first
    "user",            # a person said stop
    "cancelled",       # the run was cancelled
    "core_only",       # the mode was literal, or core did not prove
    "failed",          # core did not complete
)

#: The stops that mean "there was nothing more worth doing". Everything else is
#: an interruption and is reported as one. This tuple is what
#: `CompletionDecision.parse` checks rule 2 against.
#:
#: `unfinished` is deliberately NOT here, and it is the word shadow mode exists
#: to produce. A turn where the model stopped emitting tool calls while the
#: engine still had admitted, affordable work on the frontier did not converge
#: and did not run out: it ended with work left. Before this word existed the
#: only options were to call it convergence -- which is the lie -- or to invent
#: a budget exhaustion that had not happened.
HONEST_STOPS: Tuple[str, ...] = ("converged", "core_only")

# -- budget ----------------------------------------------------------------

#: Section 9's envelopes. `verification` is the one that cannot be borrowed
#: from, in any mode, which is why it is a line of its own rather than a
#: fraction of `core`.
BUDGET_LINES: Tuple[str, ...] = (
    "core", "verification", "bonus", "exploration", "recovery",
)

#: Which POT each line actually spends from. Only three of the five hold money.
#:
#: This map is the fix for a real defect and the reason it is a map rather than
#: prose. `ceiling()` said in its docstring that `recovery` shares the core line
#: and that `exploration` is funded from bonus -- and then metered all five
#: separately by name, so the five ceilings summed to 185% of a total that was
#: supposed to be all of it: `core` and `recovery` could each spend half of a
#: budget of one hundred and both calls passed. The mirror image was worse:
#: `exhausted("exploration")` read a meter nobody ever wrote to and reported
#: room while the bonus pot behind it was dry, which under-reported
#: `any_exhausted` -- the exact value `CompletionDecision.parse` uses to refuse
#: a dishonest `converged`.
#:
#: A line and a pot are different things and the difference is worth keeping:
#: the CALLER wants to say "this spend was recovery work", because that is what
#: the closeout reports, while the ACCOUNT has to know that recovery and core
#: come out of the same money.
FUNDING_LINES: Dict[str, str] = {
    "core": "core",
    "verification": "verification",
    "bonus": "bonus",
    "exploration": "bonus",   # maximalist explores by spending its larger share
    "recovery": "core",       # repairing a regression IS core work
}

#: The three that hold money. Their ceilings sum to the total, exactly.
FUNDED_LINES: Tuple[str, ...] = ("core", "verification", "bonus")

#: What is counted. Four units and not one, because they run out at different
#: times and a single number would hide which: a run can be out of rounds with
#: tokens to spare, and telling it to continue would be nonsense.
#:
#: This tuple is also the answer to a question the mode policy left open.
#: `CompletionPolicy.bonus_budget_share` has been in the repository for weeks
#: as `0.15 / 0.35 / 0.60` with no denominator anywhere; here the denominator
#: is stated: the share is OF THE TOTAL of each unit, so `greedy` may spend a
#: third of the turn's rounds, tokens, seconds and tool calls on bonus work,
#: and not a third of whatever happens to be left when core finishes.
SPENDABLE_UNITS: Tuple[str, ...] = ("rounds", "tool_calls", "tokens", "seconds")

# -- where candidates come from -------------------------------------------

#: Section 7's deterministic sources first, and the assisted ones named as
#: assisted. The order is the trust order: a candidate a static check produced
#: is a fact about the code, and one a model proposed is a suggestion, and the
#: scoring reads this field rather than treating them alike.
DISCOVERY_SOURCES: Tuple[str, ...] = (
    "definition_of_done",  # the contract says it and it is not done
    "static_check",        # a real finding from the analyser
    "tests",               # missing, failing or not run
    "delta",               # the Universal Delta Engine saw it
    "proof",               # `prove` came back short of proved
    "state_mirror",        # the read model says something is unfinished
    "changeset",           # an unsupported claim or an unclaimed change
    "playbook",            # the domain's own checklist
    "similar_case",        # the structural index found the same pattern
    "opportunity",         # the Opportunity Engine proposed it
    "reviewer",            # an assisted review
    "council",             # a room said so
    "model",               # a model suggested it, unassisted
    "user",                # the person asked for it mid-run
)

#: Section 5.4's `category`, for grouping in the closeout. Deliberately about
#: what the work IS, not about how good it is.
CANDIDATE_CATEGORIES: Tuple[str, ...] = (
    "verification", "correctness", "coverage", "documentation", "consistency",
    "security", "performance", "observability", "cleanup", "derivative",
    "automation", "analysis", "accessibility", "packaging",
)


def _effects() -> Tuple[str, ...]:
    """The ONE effects vocabulary, taken from where the guard reads it.

    Rule 6. `src/tool_capabilities.py::ToolEffect` is what
    `capabilities_for_tool` consults before a tool runs, so a scope envelope
    that spoke any other dialect would be describing a boundary nothing
    enforces. Imported lazily and defensively because this module is on the
    turn path and must not fail to import over a vocabulary lookup; the empty
    fallback makes `is_effect` refuse everything, which is the safe direction.
    """
    try:
        from src.tool_capabilities import ToolEffect

        return tuple(sorted(effect.value for effect in ToolEffect))
    except Exception:  # noqa: BLE001 - a contract module never fails to import
        return ()


EFFECTS: Tuple[str, ...] = _effects()


def is_effect(name: Any) -> bool:
    """Whether `name` is an effect this build can actually enforce."""
    return str(name or "") in EFFECTS


def new_id(prefix: str) -> str:
    clean = str(prefix or "").strip().rstrip("_")
    if not clean:
        raise CompletionError("prefix", "is empty; an id without a prefix is a "
                                        "hex string nobody can route", got=prefix)
    return f"{clean}_{uuid.uuid4().hex[:20]}"


def layer_rank(value: Any) -> int:
    """Position in `LAYERS`; an unknown layer ranks LAST (most optional).

    Last and not first: a layer this module does not recognise is treated as
    the least obligatory thing in the run, which is the direction that cannot
    turn an unrecognised word into work that blocks the close.
    """
    return LAYER_ORDER.get(str(value or ""), len(LAYERS) - 1)


def relation_rank(value: Any) -> int:
    """Position in `RELATIONS`; an unknown relation ranks as `unrelated`."""
    name = str(value or "")
    return RELATIONS.index(name) if name in RELATIONS else len(RELATIONS) - 1


def _score(data: Mapping[str, Any], key: str, path: str, *, default: float = 0.0) -> float:
    """A 0..1 estimate. Out of range is an error, never a clamp.

    Clamping a 1.4 would turn an estimator's arithmetic bug into a candidate
    that outranks everything, which is precisely the direction that must not
    happen quietly.
    """
    raw = data.get(key, None)
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CompletionError(f"{path}.{key}", "expected a number between 0 and 1", got=raw)
    value = float(raw)
    if value < 0.0 or value > 1.0:
        raise CompletionError(f"{path}.{key}", "is outside 0..1", got=raw)
    return round(value, 4)


def _seq(data: Mapping[str, Any], key: str, path: str) -> Sequence[Any]:
    raw = data.get(key, None)
    if raw is None:
        return ()
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise CompletionError(f"{path}.{key}", "expected a list", got=raw)
    try:
        return list(raw)
    except TypeError as exc:  # noqa: BLE001
        raise CompletionError(f"{path}.{key}", "expected a list", got=raw) from exc


def _mapping(data: Mapping[str, Any], key: str, path: str) -> Dict[str, Any]:
    raw = data.get(key, None)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CompletionError(f"{path}.{key}", "expected an object", got=raw)
    return {str(k): v for k, v in raw.items()}


def _under(marker: Any, target: Any) -> bool:
    """Whether `target` sits at or below `marker`, as paths and not strings."""
    a = str(marker or "").replace("\\", "/").strip().rstrip("/")
    b = str(target or "").replace("\\", "/").strip()
    if not a or not b:
        return False
    return b == a or b.startswith(a + "/")


def _intersect_resources(current: Sequence[str], wanted: Sequence[str]) -> Tuple[str, ...]:
    """The narrower of two working areas, by CONTAINMENT and not by equality.

    This function exists because the obvious version is wrong in the worst
    possible direction, and it was wrong here first. A set intersection of
    `("src",)` with `("src/auth.py",)` is EMPTY -- the two strings differ --
    and `ScopeEnvelope.covers()` reads an empty `allowed_resources` as "no
    working area was named", which means EVERYTHING. So the plain intersection
    turns "only touch src/auth.py" into "touch anything", which is the exact
    inversion of what the caller asked for and the one outcome the class
    docstring promises cannot happen. `PermissionEnvelope.intersect` in
    `src/agent_profiles` had already learned this; this is the same rule.

    Keeping the narrower side of each containing pair is what makes it an
    intersection: `src` ∩ `src/auth.py` is `src/auth.py`, because that is the
    set of paths both sides allow.

    An intersection that comes out empty is a REFUSAL, not an empty envelope.
    A caller narrowing to something the envelope never covered has made a
    mistake, and the alternative to saying so is handing back an envelope that
    permits the whole machine.
    """
    keep = set()
    if not current:
        keep = {str(w).replace("\\", "/").strip() for w in wanted if str(w).strip()}
    else:
        for want in wanted:
            if any(_under(have, want) for have in current):
                keep.add(str(want).replace("\\", "/").strip())
        for have in current:
            if any(_under(want, have) for want in wanted):
                keep.add(str(have).replace("\\", "/").strip())
    if not keep:
        raise CompletionError(
            "scope.allowed_resources",
            f"narrowing to {list(wanted)} leaves nothing: none of it is inside "
            f"{list(current)}. Returning an empty list here would read as `no "
            f"working area was named`, which means everything -- the opposite "
            f"of what was asked",
            got=list(wanted),
        )
    return tuple(sorted(keep))


# -- the boundary ----------------------------------------------------------


@dataclass(frozen=True)
class ScopeEnvelope:
    """Where this run may work. Section 5.2.

    Not a permission. `PermissionEnvelope` in `src/agent_profiles` says what the
    run MAY do; this says what it SHOULD do, and the two are checked in that
    order -- permission first, always, because an envelope that could widen
    authority would be a way of granting a tool by describing a goal.

    `protected_resources` and `forbidden_effects` exist beside their positive
    twins for the reason `delta_engine.ScopeRule` gives: with only an allow
    list, a forbidden path that happens to sit inside the allowed area reads as
    permitted. "Fix the login, do not touch settings.py" needs both halves.

    An envelope can be NARROWED at any time and widened only by new authority
    (§1.12.1). `narrow()` is here; there is no `widen()`, and that absence is
    the enforcement.
    """

    id: str = ""
    goal: str = ""
    intent_contract_id: str = ""
    allowed_domains: Tuple[str, ...] = ()
    allowed_resources: Tuple[str, ...] = ()
    protected_resources: Tuple[str, ...] = ()
    allowed_effects: Tuple[str, ...] = ()
    forbidden_effects: Tuple[str, ...] = ()
    allowed_relations: Tuple[str, ...] = GREEDY_RELATIONS
    ambiguities: Tuple[str, ...] = ()
    owner: str = ""
    project_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "goal", "intent_contract_id", "allowed_domains",
             "allowed_resources", "protected_resources", "allowed_effects",
             "forbidden_effects", "allowed_relations", "ambiguities", "owner",
             "project_id", "created_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "scope") -> "ScopeEnvelope":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        allowed = text_list(data, "allowed_effects", path, default=(), max_len=64)
        forbidden = text_list(data, "forbidden_effects", path, default=(), max_len=64)
        for name, values in (("allowed_effects", allowed), ("forbidden_effects", forbidden)):
            unknown = [v for v in values if EFFECTS and not is_effect(v)]
            if unknown:
                raise CompletionError(
                    f"{path}.{name}",
                    f"names effects this build cannot enforce: {unknown}. The "
                    f"vocabulary is `src/tool_capabilities.py::ToolEffect`, which "
                    f"is what the execution guard actually reads; a boundary in "
                    f"any other dialect is a boundary nothing checks",
                    got=list(values),
                )
        overlap = sorted(set(allowed) & set(forbidden))
        if overlap:
            raise CompletionError(
                f"{path}.forbidden_effects",
                f"also appear in `allowed_effects`: {overlap}; one of the two is "
                f"a mistake and guessing which would be a decision about what "
                f"this run may do to the machine",
                got=overlap,
            )
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("scope"),
            goal=text(data, "goal", path, required=False, max_len=2000),
            intent_contract_id=text(data, "intent_contract_id", path, required=False,
                                    max_len=120),
            allowed_domains=text_list(data, "allowed_domains", path, default=(), max_len=120),
            allowed_resources=text_list(data, "allowed_resources", path, default=(),
                                        max_len=512, max_items=512),
            protected_resources=text_list(data, "protected_resources", path, default=(),
                                          max_len=512, max_items=512),
            allowed_effects=allowed,
            forbidden_effects=forbidden,
            allowed_relations=text_list(data, "allowed_relations", path,
                                        default=GREEDY_RELATIONS, choices=RELATIONS,
                                        max_items=len(RELATIONS)) or GREEDY_RELATIONS,
            ambiguities=text_list(data, "ambiguities", path, default=(), max_len=500),
            owner=text(data, "owner", path, required=False, max_len=200),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "goal": self.goal,
            "intent_contract_id": self.intent_contract_id,
            "allowed_domains": list(self.allowed_domains),
            "allowed_resources": list(self.allowed_resources),
            "protected_resources": list(self.protected_resources),
            "allowed_effects": list(self.allowed_effects),
            "forbidden_effects": list(self.forbidden_effects),
            "allowed_relations": list(self.allowed_relations),
            "ambiguities": list(self.ambiguities),
            "owner": self.owner, "project_id": self.project_id,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }

    # -- the questions the frontier asks -----------------------------------

    def permits_relation(self, relation: Any) -> bool:
        return str(relation or "") in self.allowed_relations

    def permits_effect(self, effect: Any) -> bool:
        """An effect is allowed only if named allowed and not named forbidden.

        Deny wins, and an unlisted effect is DENIED when an allow list exists.
        With no allow list at all the envelope is not making a statement about
        effects and only the forbidden list applies -- which is the common case
        for a read-only errand and must not be turned into "nothing is allowed".
        """
        name = str(effect or "")
        if name in self.forbidden_effects:
            return False
        if not self.allowed_effects:
            return True
        return name in self.allowed_effects

    def protects(self, resource: Any) -> bool:
        """Whether a path or id is one the run promised not to touch."""
        return any(_under(guarded, resource) for guarded in self.protected_resources)

    def covers(self, resource: Any) -> bool:
        """Whether a path or id is inside the working area.

        An empty `allowed_resources` means the envelope did not name one, which
        is not the same as naming none: the run is bounded by permissions and
        by relation, not by a list of paths. Returning False there would refuse
        every candidate in the ordinary case.
        """
        if self.protects(resource):
            return False
        if not self.allowed_resources:
            return True
        return any(_under(allowed, resource) for allowed in self.allowed_resources)

    def narrow(self, *, resources: Sequence[str] = (), effects: Sequence[str] = (),
               relations: Sequence[str] = (), reason: str = "") -> "ScopeEnvelope":
        """A strictly smaller envelope. There is no `widen`, on purpose.

        §1.12.1: a completion mode never widens anything, and a narrowing is
        always allowed -- "only change this line" has to be able to shrink a
        greedy run. Everything here intersects; nothing is added, so no call to
        this method can ever return an envelope that permits more than the one
        it was called on.
        """
        payload = self.to_dict()
        if resources:
            payload["allowed_resources"] = list(
                _intersect_resources(self.allowed_resources, resources))
        if effects:
            current = set(self.allowed_effects) or set(EFFECTS)
            payload["allowed_effects"] = sorted(current & set(effects))
        if relations:
            payload["allowed_relations"] = [r for r in self.allowed_relations
                                            if r in set(relations)] or ["direct"]
        if reason:
            note = f"narrowed: {reason}"
            # Deduplicated because `text_list` refuses repeats and the SAME
            # narrowing arriving twice is the ordinary case, not a bug: the
            # instruction "only this file" is re-read on every round of the
            # turn, and an envelope that raised the second time would fail
            # exactly when the user was being most consistent.
            payload["ambiguities"] = list(dict.fromkeys(
                list(self.ambiguities) + [note]))
        return ScopeEnvelope.parse(payload, "scope")


# -- what was promised -----------------------------------------------------


@dataclass(frozen=True)
class CompletionContract:
    """What this run owes, written before it starts. Section 5.3.

    The point is that `definition_of_done` exists BEFORE the work, so that
    "done" is a checklist somebody wrote and not a feeling the model reports at
    the end. §7 lists "definición de terminado incompleta" as the first
    deterministic source of improvement candidates, and it can only be a source
    if it was written down first.

    `mode` is the resolved `CompletionChoice.mode` and is stored rather than
    re-resolved, so the record says which policy this run was judged under even
    after the defaults change.
    """

    id: str = ""
    mode: str = "greedy"
    policy_version: str = ""
    mode_source: str = ""
    scope_envelope_id: str = ""
    core_deliverables: Tuple[str, ...] = ()
    professional_expectations: Tuple[str, ...] = ()
    definition_of_done: Tuple[str, ...] = ()
    verification: Tuple[str, ...] = ()
    invariants: Tuple[str, ...] = ()
    playbook: str = ""
    owner: str = ""
    project_id: str = ""
    run_id: str = ""
    session_id: str = ""
    correlation_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "mode", "policy_version", "mode_source", "scope_envelope_id",
             "core_deliverables", "professional_expectations", "definition_of_done",
             "verification", "invariants", "playbook", "owner", "project_id",
             "run_id", "session_id", "correlation_id", "created_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "completion_contract") -> "CompletionContract":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        mode = one_of(data, "mode", path, choices=COMPLETION_MODES,
                      required=False, default="greedy") or "greedy"
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("completion"),
            mode=mode,
            policy_version=text(data, "policy_version", path, required=False, max_len=64)
            or policy_for(mode).policy_version,
            mode_source=text(data, "mode_source", path, required=False, max_len=64),
            scope_envelope_id=text(data, "scope_envelope_id", path, required=False,
                                   max_len=120),
            core_deliverables=text_list(data, "core_deliverables", path, default=(),
                                        max_len=500),
            professional_expectations=text_list(data, "professional_expectations", path,
                                                default=(), max_len=500),
            definition_of_done=text_list(data, "definition_of_done", path, default=(),
                                         max_len=500),
            verification=text_list(data, "verification", path, default=(), max_len=500),
            invariants=text_list(data, "invariants", path, default=(), max_len=500),
            playbook=text(data, "playbook", path, required=False, max_len=64),
            owner=text(data, "owner", path, required=False, max_len=200),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            run_id=text(data, "run_id", path, required=False, max_len=200),
            session_id=text(data, "session_id", path, required=False, max_len=200),
            correlation_id=text(data, "correlation_id", path, required=False, max_len=200),
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "mode": self.mode, "policy_version": self.policy_version,
            "mode_source": self.mode_source,
            "scope_envelope_id": self.scope_envelope_id,
            "core_deliverables": list(self.core_deliverables),
            "professional_expectations": list(self.professional_expectations),
            "definition_of_done": list(self.definition_of_done),
            "verification": list(self.verification),
            "invariants": list(self.invariants),
            "playbook": self.playbook,
            "owner": self.owner, "project_id": self.project_id,
            "run_id": self.run_id, "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }

    @property
    def policy(self) -> CompletionPolicy:
        return policy_for(self.mode)

    def layers(self) -> Tuple[str, ...]:
        """Which layers this mode is allowed to open. §4, read off the policy.

        `literal` gets `core` alone and `professional` gets one more, which is
        `max_extra_layers` used for what it says rather than a second table
        that could disagree with it.
        """
        allowed = 1 + max(0, int(self.policy.max_extra_layers))
        return LAYERS[:min(allowed, len(LAYERS))]

    def fingerprint(self) -> str:
        return fingerprint([
            ("mode", self.mode),
            ("policy", self.policy_version),
            ("core", list(self.core_deliverables)),
            ("done", list(self.definition_of_done)),
            ("verification", list(self.verification)),
            ("invariants", list(self.invariants)),
            ("scope", self.scope_envelope_id),
        ])


# -- one improvement -------------------------------------------------------


@dataclass(frozen=True)
class ImprovementCandidate:
    """One thing that could be done next, and what it would cost. Section 5.4.

    Rule 1 of the module docstring lives in this class's SHAPE:
    `required_permissions` says what this would NEED, and there is no field
    that could grant one. The engine intersects that list against the run's
    `PermissionEnvelope` before scoring, so an expensive candidate never gets
    the chance to argue its way past a missing permission with a high value.

    `evidence_refs` is what separates a candidate from a wish. §30: "no afirmar
    required sin relación demostrable" -- a candidate on the `core` or
    `professional` layer with no evidence behind it is refused here, because
    those two layers are the ones that can block a close, and something that
    can block a close on nobody's evidence is an opinion with a veto.
    """

    id: str = ""
    title: str = ""
    layer: str = "bonus"
    category: str = "correctness"
    relation: str = "adjacent"
    source: str = "model"
    expected_value: float = 0.0
    estimated_cost: float = 0.0
    risk: float = 0.0
    confidence: float = 0.0
    reversibility: str = "full"
    status: str = "candidate"
    rejection_reason: str = ""
    detail: str = ""
    resources: Tuple[str, ...] = ()
    required_permissions: Tuple[str, ...] = ()
    required_effects: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    verification: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    dedupe_key: str = ""
    created_at: str = ""

    _KEYS = ("id", "title", "layer", "category", "relation", "source",
             "expected_value", "estimated_cost", "risk", "confidence",
             "reversibility", "status", "rejection_reason", "detail", "resources",
             "required_permissions", "required_effects", "dependencies",
             "verification", "evidence_refs", "dedupe_key", "created_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "candidate") -> "ImprovementCandidate":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        layer = one_of(data, "layer", path, choices=LAYERS, required=False,
                       default="bonus") or "bonus"
        status = one_of(data, "status", path, choices=CANDIDATE_STATUSES,
                        required=False, default="candidate") or "candidate"
        reason = one_of(data, "rejection_reason", path, choices=REJECTION_REASONS,
                        required=False, default="") or ""
        if status == "rejected" and not reason:
            raise CompletionError(
                f"{path}.rejection_reason",
                "is empty on a rejected candidate; a rejection nobody recorded "
                "comes back next round and is rejected again, forever",
                got=data.get("rejection_reason"),
            )
        if reason and status not in ("rejected", "deferred"):
            raise CompletionError(
                f"{path}.rejection_reason",
                f"is set while the status is `{status}`; a reason to refuse and "
                f"a decision to run are contradictory in the same row",
                got=reason,
            )
        evidence = text_list(data, "evidence_refs", path, default=(), max_len=512)
        if layer in REQUIRED_LAYERS and not evidence:
            raise CompletionError(
                f"{path}.evidence_refs",
                f"is empty on a `{layer}` candidate; that layer can block the "
                f"close, and something able to block a close on nobody's "
                f"evidence is an opinion with a veto",
                got=data.get("evidence_refs"),
            )
        effects = text_list(data, "required_effects", path, default=(), max_len=64)
        unknown = [e for e in effects if EFFECTS and not is_effect(e)]
        if unknown:
            raise CompletionError(
                f"{path}.required_effects",
                f"names effects this build cannot enforce: {unknown}; the "
                f"vocabulary is `src/tool_capabilities.py::ToolEffect`",
                got=list(effects),
            )
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("improvement"),
            title=text(data, "title", path, max_len=300),
            layer=layer,
            category=one_of(data, "category", path, choices=CANDIDATE_CATEGORIES,
                            required=False, default="correctness") or "correctness",
            relation=one_of(data, "relation", path, choices=RELATIONS,
                            required=False, default="adjacent") or "adjacent",
            source=one_of(data, "source", path, choices=DISCOVERY_SOURCES,
                          required=False, default="model") or "model",
            expected_value=_score(data, "expected_value", path),
            estimated_cost=_score(data, "estimated_cost", path),
            risk=_score(data, "risk", path),
            confidence=_score(data, "confidence", path),
            reversibility=one_of(data, "reversibility", path, choices=REVERSIBILITY,
                                 required=False, default="full") or "full",
            status=status,
            rejection_reason=reason,
            detail=text(data, "detail", path, required=False, max_len=2000),
            resources=text_list(data, "resources", path, default=(), max_len=512),
            required_permissions=text_list(data, "required_permissions", path,
                                           default=(), max_len=120),
            required_effects=effects,
            dependencies=text_list(data, "dependencies", path, default=(), max_len=120),
            verification=text_list(data, "verification", path, default=(), max_len=500),
            evidence_refs=evidence,
            dedupe_key=text(data, "dedupe_key", path, required=False, max_len=300),
            created_at=timestamp(data, "created_at", path) or now_iso(),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id, "title": self.title, "layer": self.layer,
            "category": self.category, "relation": self.relation,
            "source": self.source, "expected_value": self.expected_value,
            "estimated_cost": self.estimated_cost, "risk": self.risk,
            "confidence": self.confidence, "reversibility": self.reversibility,
            "status": self.status, "created_at": self.created_at,
        }
        for key in ("rejection_reason", "detail", "dedupe_key"):
            value = getattr(self, key)
            if value:
                out[key] = value
        for key in ("resources", "required_permissions", "required_effects",
                    "dependencies", "verification", "evidence_refs"):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        return out

    @property
    def key(self) -> str:
        """What makes two candidates the same. §Frontier: dedupe.

        The title is the fallback and not the key: two generators describing
        the same missing test in different words are one candidate, and only a
        structural key can say so. A generator that supplies `dedupe_key` gets
        exact dedupe; one that does not gets the coarse version and is expected
        to collide occasionally rather than never.
        """
        if self.dedupe_key:
            return self.dedupe_key
        return f"{self.layer}:{self.category}:{'|'.join(sorted(self.resources))}"

    @property
    def actionable(self) -> bool:
        return self.status in ("candidate", "selected")

    @property
    def safe_to_run_unasked(self) -> bool:
        """§12's list, as one property, and every clause is load-bearing.

        Local, reversible, inside the envelope's own relations, needing no new
        effect, and with a verification attached. The engine checks the
        envelope and the permissions separately -- this is only the part that
        depends on the candidate itself.
        """
        return (
            self.reversibility == "full"
            and self.relation in GREEDY_RELATIONS
            and not self.required_effects
            and bool(self.verification)
            and self.risk <= 0.2
        )


# -- the budget ------------------------------------------------------------


@dataclass(frozen=True)
class BudgetSpend:
    """What has been spent, per unit. Never a remainder.

    The council's `BudgetState` says it first and it is worth repeating here:
    *every field is a spend, not a remainder*. A remainder is a derived number
    that goes wrong the moment two writers disagree about the total, and the
    question a caller asks -- "may I spend this?" -- is answered from the spend
    and the ceiling together, never from a stored remainder somebody forgot to
    update.
    """

    rounds: int = 0
    tool_calls: int = 0
    tokens: int = 0
    seconds: float = 0.0

    _KEYS = SPENDABLE_UNITS

    @classmethod
    def parse(cls, raw: Any, path: str = "spend") -> "BudgetSpend":
        data = as_mapping(raw, path) if raw is not None else {}
        reject_unknown(data, cls._KEYS, path)
        seconds = data.get("seconds", 0.0) or 0.0
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise CompletionError(f"{path}.seconds", "expected a number", got=seconds)
        return cls(
            rounds=whole(data, "rounds", path, default=0, minimum=0) or 0,
            tool_calls=whole(data, "tool_calls", path, default=0, minimum=0) or 0,
            tokens=whole(data, "tokens", path, default=0, minimum=0) or 0,
            seconds=max(0.0, round(float(seconds), 3)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"rounds": self.rounds, "tool_calls": self.tool_calls,
                "tokens": self.tokens, "seconds": self.seconds}

    def plus(self, other: "BudgetSpend") -> "BudgetSpend":
        return BudgetSpend(
            rounds=self.rounds + other.rounds,
            tool_calls=self.tool_calls + other.tool_calls,
            tokens=self.tokens + other.tokens,
            seconds=round(self.seconds + other.seconds, 3),
        )

    def get(self, unit: str) -> float:
        return float(getattr(self, str(unit), 0) or 0)

    @property
    def empty(self) -> bool:
        return not (self.rounds or self.tool_calls or self.tokens or self.seconds)


@dataclass(frozen=True)
class CompletionBudget:
    """The ceilings, the reserve, and the answer to "may I spend this?".

    Rule 4. `verification` is a line of its own and `may_spend()` refuses to
    fund anything else from it, in EVERY mode -- `CompletionPolicy` sets
    `requires_verification=True` for all four, and the comment beside it says
    why: depth is negotiable, evidence is not. A `literal` run has the narrowest
    reserve of the four and not a zero one.

    `bonus` is derived from `CompletionPolicy.bonus_budget_share` times the
    TOTAL, which is the denominator that fraction has been missing since it was
    written. A share of "whatever is left when core finishes" would mean a run
    that overspends on core gets a bigger bonus allowance, which is exactly
    backwards.
    """

    total: BudgetSpend = None  # type: ignore[assignment]
    reserve_share: float = 0.15
    bonus_share: float = 0.0
    spent: Mapping[str, BudgetSpend] = None  # type: ignore[assignment]

    _KEYS = ("total", "reserve_share", "bonus_share", "spent")

    def __post_init__(self) -> None:
        if self.total is None:
            object.__setattr__(self, "total", BudgetSpend())
        if self.spent is None:
            object.__setattr__(self, "spent", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "budget") -> "CompletionBudget":
        data = as_mapping(raw, path) if raw is not None else {}
        reject_unknown(data, cls._KEYS, path)
        spent_raw = _mapping(data, "spent", path)
        spent: Dict[str, BudgetSpend] = {}
        for line, value in spent_raw.items():
            if line not in BUDGET_LINES:
                raise CompletionError(
                    f"{path}.spent.{line}",
                    f"is not a budget line; known: {list(BUDGET_LINES)}", got=line)
            spent[line] = BudgetSpend.parse(value, f"{path}.spent.{line}")
        return cls(
            total=BudgetSpend.parse(data.get("total"), f"{path}.total"),
            reserve_share=_score(data, "reserve_share", path, default=0.15),
            bonus_share=_score(data, "bonus_share", path, default=0.0),
            spent=spent,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total.to_dict(),
            "reserve_share": self.reserve_share,
            "bonus_share": self.bonus_share,
            "spent": {line: value.to_dict() for line, value in self.spent.items()},
        }

    @classmethod
    def for_policy(cls, policy: CompletionPolicy, total: BudgetSpend, *,
                   reserve_share: float = 0.15) -> "CompletionBudget":
        """The budget a mode implies. The one place the share gets a denominator."""
        return cls(total=total, reserve_share=reserve_share,
                   bonus_share=float(policy.bonus_budget_share), spent={})

    # -- the arithmetic ----------------------------------------------------

    def ceiling(self, line: str, unit: str) -> float:
        """How much of `unit` the POT behind `line` holds, out of the total.

        Three pots, and their ceilings add up to exactly the total: the reserve,
        the bonus share, and core with whatever is left. `recovery` reads the
        core ceiling and `exploration` reads the bonus one, because they spend
        that money rather than money of their own -- see `FUNDING_LINES` for
        what happened when the two were metered apart.
        """
        pot = FUNDING_LINES.get(str(line), "core")
        total = self.total.get(unit)
        if total <= 0:
            return 0.0
        reserve = total * max(0.0, min(1.0, self.reserve_share))
        bonus = total * max(0.0, min(1.0, self.bonus_share))
        if pot == "verification":
            return round(reserve, 3)
        if pot == "bonus":
            return round(bonus, 3)
        return round(max(0.0, total - reserve - bonus), 3)

    def used(self, line: str, unit: str) -> float:
        """What the POT behind `line` has spent -- every line that draws on it.

        Summing across the aliases rather than reading one key is the whole
        point: `spend("recovery", ...)` and `spend("core", ...)` come out of one
        pot, so a reader of either has to see both, or the second call is
        approved against money the first already took.
        """
        pot = FUNDING_LINES.get(str(line), "core")
        total = 0.0
        for name, row in self.spent.items():
            if FUNDING_LINES.get(name, "core") == pot:
                total += row.get(unit)
        return round(total, 3)

    def remaining(self, line: str, unit: str) -> float:
        return round(self.ceiling(line, unit) - self.used(line, unit), 3)

    def exhausted(self, line: str) -> Tuple[str, ...]:
        """Which units of a line have been spent to zero. Empty means room.

        A line whose ceiling is ZERO is not exhausted -- it was never opened.
        The distinction is not pedantry: `literal` has `bonus_share = 0.0`, so
        without it every literal run would report its bonus line as spent, and
        `CompletionDecision.parse` would then refuse `core_only` -- the one
        honest stop a literal run can have -- on the grounds that it ran out of
        a budget it was never given. Ran out and never started are different
        facts, and this engine exists to keep exactly that kind of pair apart.
        """
        out = []
        for unit in SPENDABLE_UNITS:
            if self.total.get(unit) <= 0:
                continue
            if self.ceiling(line, unit) <= 0:
                continue
            if self.remaining(line, unit) <= 0:
                out.append(unit)
        return tuple(out)

    def may_spend(self, line: str, want: BudgetSpend) -> Tuple[bool, str]:
        """`(ok, reason)`. The reserve is untouchable by anything but itself.

        Refusing rather than allowing-and-reporting is the point: §9's rule is
        "nunca gastar verification reserve en bonus", and a check that permitted
        the spend and noted it would be a rule with a log line instead of a
        rule.
        """
        if line not in BUDGET_LINES:
            return False, f"unknown budget line {line!r}"
        for unit in SPENDABLE_UNITS:
            asked = want.get(unit)
            if asked <= 0:
                continue
            if self.total.get(unit) <= 0:
                # No ceiling declared for this unit: this budget is not
                # counting it, which is not the same as it being exhausted.
                continue
            if self.remaining(line, unit) < asked:
                return False, f"{line} budget has no {unit} left"
        return True, ""

    def spend(self, line: str, amount: BudgetSpend) -> "CompletionBudget":
        """A NEW budget with the spend recorded. Frozen all the way down."""
        if line not in BUDGET_LINES:
            raise CompletionError("line", f"is not one of {list(BUDGET_LINES)}", got=line)
        merged = dict(self.spent)
        merged[line] = self.spent.get(line, BudgetSpend()).plus(amount)
        return CompletionBudget(total=self.total, reserve_share=self.reserve_share,
                                bonus_share=self.bonus_share, spent=merged)

    @property
    def any_exhausted(self) -> bool:
        """Whether any POT that had a ceiling has run out. Rule 2 reads this.

        Over the three funded pots and not the five lines: iterating the lines
        asks the same pot twice and would answer identically, but iterating the
        pots is what makes it obvious that there are three and not five.
        """
        return any(self.exhausted(line) for line in FUNDED_LINES)


# -- the decision ----------------------------------------------------------


@dataclass(frozen=True)
class CompletionDecision:
    """Why the run stopped where it stopped. Section 1.4, and rule 2.

    This is the object that makes over-delivery auditable: `completed_layers`
    says how far it went, `executed` and `rejected` say what was added and what
    was refused and why, `stop_reason` says why it ended, and
    `degraded_integrations` says which of the systems it would have consulted
    were switched off -- because a frontier computed without the Delta Engine
    is a frontier that could not see scope creep, and reporting that as a clean
    stop would be the same lie as `preserved` without an observation.

    `parse` enforces rule 2: `converged` is refused while a budget line is
    exhausted. Those two sentences lead to opposite next actions -- one says
    "there was nothing more worth doing", the other says "raise the budget" --
    and only one of them can be true.
    """

    id: str = ""
    contract_id: str = ""
    scope_envelope_id: str = ""
    mode: str = "greedy"
    policy_version: str = ""
    completed_layers: Tuple[str, ...] = ()
    stop_reason: str = "converged"
    stop_detail: str = ""
    executed: Tuple[ImprovementCandidate, ...] = ()
    rejected: Tuple[ImprovementCandidate, ...] = ()
    deferred: Tuple[ImprovementCandidate, ...] = ()
    budget: CompletionBudget = None  # type: ignore[assignment]
    proof_refs: Tuple[str, ...] = ()
    delta_refs: Tuple[str, ...] = ()
    changeset_refs: Tuple[str, ...] = ()
    degraded_integrations: Tuple[str, ...] = ()
    shadow: bool = False
    owner: str = ""
    project_id: str = ""
    run_id: str = ""
    session_id: str = ""
    correlation_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "contract_id", "scope_envelope_id", "mode", "policy_version",
             "completed_layers", "stop_reason", "stop_detail", "executed",
             "rejected", "deferred", "budget", "proof_refs", "delta_refs",
             "changeset_refs", "degraded_integrations", "shadow", "owner",
             "project_id", "run_id", "session_id", "correlation_id",
             "created_at", "schema_version")

    def __post_init__(self) -> None:
        if self.budget is None:
            object.__setattr__(self, "budget", CompletionBudget())

    @classmethod
    def parse(cls, raw: Any, path: str = "decision") -> "CompletionDecision":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        stop = one_of(data, "stop_reason", path, choices=STOP_REASONS,
                      required=False, default="converged") or "converged"
        budget = CompletionBudget.parse(data.get("budget"), f"{path}.budget")
        if stop in HONEST_STOPS and budget.any_exhausted:
            spent = sorted({line for line in BUDGET_LINES if budget.exhausted(line)})
            raise CompletionError(
                f"{path}.stop_reason",
                f"is `{stop}` while the {spent} budget line(s) are exhausted; a "
                f"run that ran out did not converge, and calling it convergence "
                f"is what stops anyone from raising the budget",
                got=stop,
            )
        layers = text_list(data, "completed_layers", path, default=(), choices=LAYERS,
                           max_items=len(LAYERS))
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("decision"),
            contract_id=text(data, "contract_id", path, required=False, max_len=120),
            scope_envelope_id=text(data, "scope_envelope_id", path, required=False,
                                   max_len=120),
            mode=one_of(data, "mode", path, choices=COMPLETION_MODES, required=False,
                        default="greedy") or "greedy",
            policy_version=text(data, "policy_version", path, required=False, max_len=64),
            completed_layers=layers,
            stop_reason=stop,
            stop_detail=text(data, "stop_detail", path, required=False, max_len=1000),
            executed=tuple(ImprovementCandidate.parse(item, f"{path}.executed[{i}]")
                           for i, item in enumerate(_seq(data, "executed", path))),
            rejected=tuple(ImprovementCandidate.parse(item, f"{path}.rejected[{i}]")
                           for i, item in enumerate(_seq(data, "rejected", path))),
            deferred=tuple(ImprovementCandidate.parse(item, f"{path}.deferred[{i}]")
                           for i, item in enumerate(_seq(data, "deferred", path))),
            budget=budget,
            proof_refs=text_list(data, "proof_refs", path, default=(), max_len=200),
            delta_refs=text_list(data, "delta_refs", path, default=(), max_len=200),
            changeset_refs=text_list(data, "changeset_refs", path, default=(), max_len=200),
            degraded_integrations=text_list(data, "degraded_integrations", path,
                                            default=(), max_len=120),
            shadow=flag(data, "shadow", path, default=False),
            owner=text(data, "owner", path, required=False, max_len=200),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            run_id=text(data, "run_id", path, required=False, max_len=200),
            session_id=text(data, "session_id", path, required=False, max_len=200),
            correlation_id=text(data, "correlation_id", path, required=False, max_len=200),
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "contract_id": self.contract_id,
            "scope_envelope_id": self.scope_envelope_id,
            "mode": self.mode, "policy_version": self.policy_version,
            "completed_layers": list(self.completed_layers),
            "stop_reason": self.stop_reason, "stop_detail": self.stop_detail,
            "executed": [c.to_dict() for c in self.executed],
            "rejected": [c.to_dict() for c in self.rejected],
            "deferred": [c.to_dict() for c in self.deferred],
            "budget": self.budget.to_dict(),
            "proof_refs": list(self.proof_refs),
            "delta_refs": list(self.delta_refs),
            "changeset_refs": list(self.changeset_refs),
            "degraded_integrations": list(self.degraded_integrations),
            "shadow": self.shadow,
            "owner": self.owner, "project_id": self.project_id,
            "run_id": self.run_id, "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }

    # -- the closeout, computed and never stored ---------------------------

    def by_layer(self, layer: str) -> Tuple[ImprovementCandidate, ...]:
        return tuple(c for c in self.executed if c.layer == layer)

    def extras(self) -> Tuple[ImprovementCandidate, ...]:
        """What was added beyond what was asked. §30: never hidden in the core.

        `bonus` and `exploratory` only. The whole reason the layers exist is so
        that this method can answer honestly, and a closeout that folded the
        two lists together would leave the reader unable to tell what they got.
        """
        return tuple(c for c in self.executed if c.layer in ("bonus", "exploratory"))

    def summary(self) -> Dict[str, Any]:
        counts = {layer: len(self.by_layer(layer)) for layer in LAYERS}
        reasons: Dict[str, int] = {}
        for candidate in self.rejected:
            key = candidate.rejection_reason or "unknown"
            reasons[key] = reasons.get(key, 0) + 1
        return {
            "mode": self.mode,
            "layers": list(self.completed_layers),
            "executed": counts,
            "extras": len(self.extras()),
            "rejected": len(self.rejected),
            "deferred": len(self.deferred),
            "rejection_reasons": reasons,
            "stop_reason": self.stop_reason,
            "honest_stop": self.stop_reason in HONEST_STOPS,
            "degraded": list(self.degraded_integrations),
            "shadow": self.shadow,
        }

    def fingerprint(self) -> str:
        return fingerprint([
            ("contract", self.contract_id),
            ("scope", self.scope_envelope_id),
            ("mode", self.mode),
            ("layers", list(self.completed_layers)),
            ("stop", self.stop_reason),
            ("executed", sorted(c.id for c in self.executed)),
        ])
