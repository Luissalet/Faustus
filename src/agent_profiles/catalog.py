"""
agent_profiles/catalog.py — the five things an agent references, as data.

An `AgentDef` says who works.  It should not also carry, inline, the list of
checks its work owes, the shape of the context it wants, the ceiling on what it
may spend, how it behaves in a room with other agents, and the schema of what
it hands back.  Those five belong to the plan's §9-§13 and they belong here,
versioned, so that a run receipt can name `full_delivery_v1` instead of
embedding a blob whose meaning drifted three commits ago.

Four properties are deliberate and each of them is a rule someone will
otherwise be tempted to break:

**A profile is inert.**  Every type in this file is a frozen dataclass of
strings, numbers, booleans and tuples.  A verification profile *declares*
checks; it has no callable, no hook and no place to put one, so it cannot
quietly become the thing that runs them (§9).  `prove` keeps the verdict.

**A profile grants nothing.**  `may_delegate=True` in a collaboration profile
is a statement about the role, not a capability: whether this agent can
actually spawn a subagent is still decided by the permission envelope and the
depth ceiling (§12).  Same for `allows_network` and `allows_gpu` in a budget:
they are ceilings that a run may lower, never keys that open anything.

**A reduction only reduces.**  `reduce_budget` is the one function here that
takes untrusted input, and §11/§15 are explicit: a run may shrink its budget,
and asking for more is not an error but is not granted either.  So the ignored
fields come back *in the result*, named, instead of being dropped in silence.

**"Stricter" is not `max`.**  For verification, stricter means more checks and
more blocking ones.  For a budget it means fewer tokens, less time, fewer
branches.  A single comparison helper would get one of the two backwards, so
each kind declares its own ordering next to the type it orders.

The catalogue does not import the context engine.  A `ContextProfileRef` names
an engine profile by id, and the engine owns what that id means (§10); the
coupling is checked by `tests/test_agent_profile_catalog.py`, which imports the
real `src.context_engine.budgets.PROFILES` and fails if a reference dangles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields as dataclass_fields, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .contracts import ProfileError

logger = logging.getLogger(__name__)

__all__ = [
    "VerificationProfile",
    "ContextProfileRef",
    "BudgetProfile",
    "CollaborationProfile",
    "OutputContract",
    "VERIFICATION_PROFILES",
    "CONTEXT_PROFILES",
    "BUDGET_PROFILES",
    "COLLABORATION_PROFILES",
    "OUTPUT_CONTRACTS",
    "KINDS",
    "SPEAK_POLICIES",
    "WRITE_POLICIES",
    "get",
    "require",
    "exists",
    "list_profiles",
    "register",
    "stricter",
    "reduce_budget",
]

#: The five kinds, in the order `ProfileSet` lists them.
KINDS: Tuple[str, ...] = (
    "context",
    "verification",
    "budget",
    "collaboration",
    "output",
)

#: Least permissive first.  Used to order collaboration profiles by strictness
#: and to reject a typo at registration instead of at 3am in a Council session.
WRITE_POLICIES: Tuple[str, ...] = (
    "read_only", "propose_only", "scoped_write", "write",
)
SPEAK_POLICIES: Tuple[str, ...] = (
    "on_assignment",
    "on_assignment_or_blocking_finding",
    "on_checkpoint",
    "on_request",
    "free",
)


# ── the five types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerificationProfile:
    """Which checks a run owes, and which of them can stop it (§9).

    `checks` is a declaration, not a plan of execution: the runtime resolves
    each name against the capabilities it actually has, and says so when one is
    unavailable.  `blocking` is the subset whose failure is a rejection rather
    than a note; it must be a subset of `checks`, because a blocking check
    nobody declared is a rule that appears out of nowhere.

    There is no callable in this type and there must never be one.
    """

    id: str
    version: str
    checks: Tuple[str, ...]
    blocking: Tuple[str, ...]
    requires_reviewer: bool
    description: str


@dataclass(frozen=True)
class ContextProfileRef:
    """A pointer into the Context Engine, plus the ceiling to ask it for (§10).

    Deliberately a *reference*.  The engine owns section shares, priorities and
    trimming; duplicating any of that here would create a second answer to "what
    was this model told", which is the one thing `src/context_engine` exists to
    prevent.
    """

    id: str
    version: str
    engine_profile_id: str
    budget_tokens: int
    description: str


@dataclass(frozen=True)
class BudgetProfile:
    """Ceilings, never grants (§11).

    `allows_network` and `allows_gpu` say what the budget *tolerates*; the
    permission envelope says what is actually reachable, and the intersection
    is what happens.  A budget that says `allows_network=True` for an agent
    denied every network tool changes nothing at all, which is the correct
    outcome and worth stating because the field name invites the other reading.

    `ignored` is empty for every catalogue entry.  It is filled only by
    `reduce_budget`, which uses it to hand back the parts of a patch it refused
    to grant — see that function.
    """

    id: str
    version: str
    max_tokens: int
    max_seconds: int
    max_rounds: int
    max_tool_calls: int
    max_branches: int
    max_retries: int
    allows_network: bool
    allows_gpu: bool
    description: str
    ignored: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CollaborationProfile:
    """How an agent behaves in a room with other agents (§12).

    `may_lead` and `may_delegate` describe the role this profile is written
    for.  They do not confer the capability: delegation is still computed from
    the permission envelope and the depth ceiling, and a profile that says
    `may_delegate=True` for an agent whose envelope has no subagent tool simply
    describes a role it cannot currently play.  Never read this field as an
    authorisation.
    """

    id: str
    version: str
    may_lead: bool
    may_delegate: bool
    speak_policy: str
    write_policy: str
    review_own_work: bool
    preferred_roles: Tuple[str, ...]
    handoff_contract: str
    description: str


@dataclass(frozen=True)
class OutputContract:
    """The shape of a handoff (§13). Shape only — it cannot fabricate proof."""

    id: str
    version: str
    fields: Tuple[str, ...]
    required: Tuple[str, ...]
    description: str


# ── verification (§9) ──────────────────────────────────────────────────────
#
# `default` is the one id without a `_v1` suffix, on purpose: it is the name an
# AgentDef falls back to when it declares nothing, so it has to stay stable
# across versions of its content. Its `version` field carries the revision
# instead. Every other id is versioned in the name, so a definition written
# today keeps meaning what it meant when it was written.

VERIFICATION_PROFILES: Dict[str, VerificationProfile] = {
    "default": VerificationProfile(
        id="default",
        version="v1",
        checks=("changeset", "prove"),
        blocking=("prove",),
        requires_reviewer=False,
        description="The floor every run owes: a changeset and a prove verdict.",
    ),
    "targeted_tests_v1": VerificationProfile(
        id="targeted_tests_v1",
        version="v1",
        checks=("targeted_tests", "delta_scope", "changeset", "prove"),
        blocking=("targeted_tests", "delta_scope", "prove"),
        requires_reviewer=False,
        description="Surgical work: the tests that touch the change, and nothing wandered.",
    ),
    "full_delivery_v1": VerificationProfile(
        id="full_delivery_v1",
        version="v1",
        checks=(
            "targeted_tests", "affected_suite", "lint", "typecheck",
            "artifact_integrity", "delta_scope", "changeset", "prove",
        ),
        blocking=("targeted_tests", "affected_suite", "delta_scope", "prove"),
        requires_reviewer=True,
        description="Finished delivery: suite, static checks, artifacts, reviewer by risk.",
    ),
    "security_review_v1": VerificationProfile(
        id="security_review_v1",
        version="v1",
        checks=(
            "permissions_effects", "secrets_scan", "network_egress",
            "auth_paths", "untrusted_inputs", "dependency_diff", "prove",
        ),
        blocking=("permissions_effects", "secrets_scan", "auth_paths", "prove"),
        requires_reviewer=True,
        description="Adversarial review: permissions, secrets, egress, auth, inputs, deps.",
    ),
    "image_quality_v1": VerificationProfile(
        id="image_quality_v1",
        version="v1",
        checks=(
            "format_resolution", "prompt_adherence",
            "identity_composition_anatomy", "incidental_changes",
            "recipe_lineage", "quality_evaluator",
        ),
        blocking=("format_resolution", "recipe_lineage"),
        requires_reviewer=False,
        description="Generated imagery: adherence, anatomy, no incidental drift, lineage kept.",
    ),
    "incident_containment_v1": VerificationProfile(
        id="incident_containment_v1",
        version="v1",
        checks=(
            "service_restored", "change_is_reversible", "blast_radius",
            "timeline_recorded", "prove",
        ),
        blocking=("service_restored", "change_is_reversible", "prove"),
        requires_reviewer=False,
        description="Contain first: restore service with a reversible change, log the timeline.",
    ),
    "document_evidence_v1": VerificationProfile(
        id="document_evidence_v1",
        version="v1",
        checks=(
            "source_available", "citations_resolve", "schema_conformance",
            "delta_scope", "prove",
        ),
        blocking=("citations_resolve", "schema_conformance", "prove"),
        requires_reviewer=False,
        description="Document work: every citation resolves and the schema holds.",
    ),
}


# ── context (§10) ──────────────────────────────────────────────────────────
#
# The Context Engine ships six profiles: balanced_v1, conversation_v1,
# code_review_v1, research_v1, media_v1 and voice_v1. The plan names seven
# context profiles that do not map one-to-one onto them, and the right answer
# is to point the extras at the nearest real profile and say so — inventing a
# seventh engine profile from here would put a second author on a file this
# package does not own (§1.2). Where a mapping is approximate the comment says
# what is being approximated, so the day someone adds the missing lane knows
# which references to move.

CONTEXT_PROFILES: Dict[str, ContextProfileRef] = {
    "default": ContextProfileRef(
        id="default",
        version="v1",
        engine_profile_id="balanced_v1",
        budget_tokens=48_000,
        description="Agentic default: instructions, goal, some evidence, a little memory.",
    ),
    # Exact-ish: code_review_v1 already weights code map and retrieved docs
    # over memory. "Narrow" is expressed by the token ceiling, not by a
    # different share table.
    "narrow_code_v1": ContextProfileRef(
        id="narrow_code_v1",
        version="v1",
        engine_profile_id="code_review_v1",
        budget_tokens=24_000,
        description="Goal, the symbols and files it names, and their direct tests.",
    ),
    # Approximate: the engine has no architecture lane. code_review_v1 is the
    # only profile that funds the code map heavily, which is what a map,
    # contracts and dependencies need; the wider ceiling does the rest.
    "architecture_code_v1": ContextProfileRef(
        id="architecture_code_v1",
        version="v1",
        engine_profile_id="code_review_v1",
        budget_tokens=96_000,
        description="Repo map, contracts and dependencies rather than one file's detail.",
    ),
    # Exact: research_v1 is documents-dominant with the code map at zero.
    "research_primary_sources_v1": ContextProfileRef(
        id="research_primary_sources_v1",
        version="v1",
        engine_profile_id="research_v1",
        budget_tokens=128_000,
        description="Primary sources and the places they contradict each other.",
    ),
    # Exact: media_v1 is the recipe-and-references profile.
    "creative_reference_pack_v1": ContextProfileRef(
        id="creative_reference_pack_v1",
        version="v1",
        engine_profile_id="media_v1",
        budget_tokens=32_000,
        description="References, recipes and stated preferences for generation work.",
    ),
    # Approximate: no security lane in the engine. A security review reads a
    # diff, symbols and project rules, which is code_review_v1's exact shape;
    # the threat model and past incidents arrive as retrieved documents inside
    # that same share.
    "security_review_v1": ContextProfileRef(
        id="security_review_v1",
        version="v1",
        engine_profile_id="code_review_v1",
        budget_tokens=64_000,
        description="Diff, threat model, effective permissions and prior incidents.",
    ),
    # Approximate: no incident lane. balanced_v1 is the only profile that funds
    # current state and decisions alongside retrieval, which is what a timeline
    # plus logs plus the last changes need. conversation_v1 was the other
    # candidate and loses: it spends 45% on chat history and nothing on state.
    "incident_v1": ContextProfileRef(
        id="incident_v1",
        version="v1",
        engine_profile_id="balanced_v1",
        budget_tokens=32_000,
        description="What is broken now: state, timeline, logs and the last changes.",
    ),
    # Approximate: no document-evidence lane. research_v1 is documents-first,
    # which is the dominant need; the schema and deltas ride in project rules
    # and current state.
    "document_evidence_v1": ContextProfileRef(
        id="document_evidence_v1",
        version="v1",
        engine_profile_id="research_v1",
        budget_tokens=96_000,
        description="The source document, its citations, the target schema and the deltas.",
    ),
}


# ── budget (§11) ───────────────────────────────────────────────────────────

BUDGET_PROFILES: Dict[str, BudgetProfile] = {
    "default": BudgetProfile(
        id="default", version="v1",
        max_tokens=60_000, max_seconds=900, max_rounds=12,
        max_tool_calls=60, max_branches=1, max_retries=1,
        allows_network=False, allows_gpu=False,
        description="Conservative floor for a definition that declares nothing.",
    ),
    "fast_local_v1": BudgetProfile(
        id="fast_local_v1", version="v1",
        max_tokens=24_000, max_seconds=180, max_rounds=6,
        max_tool_calls=24, max_branches=1, max_retries=1,
        allows_network=False, allows_gpu=True,
        description="Local model, answer in seconds, no egress.",
    ),
    "standard_v1": BudgetProfile(
        id="standard_v1", version="v1",
        max_tokens=96_000, max_seconds=1_800, max_rounds=20,
        max_tool_calls=120, max_branches=2, max_retries=2,
        allows_network=True, allows_gpu=True,
        description="An ordinary task: half an hour, a couple of retries.",
    ),
    "deep_research_v1": BudgetProfile(
        id="deep_research_v1", version="v1",
        max_tokens=400_000, max_seconds=7_200, max_rounds=60,
        max_tool_calls=400, max_branches=4, max_retries=3,
        allows_network=True, allows_gpu=False,
        description="Many sources, many rounds; reading, not generating.",
    ),
    "creative_preview_v1": BudgetProfile(
        id="creative_preview_v1", version="v1",
        max_tokens=32_000, max_seconds=900, max_rounds=10,
        max_tool_calls=80, max_branches=4, max_retries=2,
        allows_network=False, allows_gpu=True,
        description="Cheap variants first: branches are the point, quality comes later.",
    ),
    "max_quality_v1": BudgetProfile(
        id="max_quality_v1", version="v1",
        max_tokens=600_000, max_seconds=14_400, max_rounds=80,
        max_tool_calls=600, max_branches=6, max_retries=4,
        allows_network=True, allows_gpu=True,
        description="The expensive lane: used when the result outlives the bill.",
    ),
    "overnight_local_v1": BudgetProfile(
        id="overnight_local_v1", version="v1",
        max_tokens=1_200_000, max_seconds=43_200, max_rounds=200,
        max_tool_calls=2_000, max_branches=8, max_retries=6,
        allows_network=False, allows_gpu=True,
        description="Twelve hours of local compute, checkpointed, no egress.",
    ),
    "reviewer_standard_v1": BudgetProfile(
        id="reviewer_standard_v1", version="v1",
        max_tokens=120_000, max_seconds=1_800, max_rounds=12,
        max_tool_calls=80, max_branches=1, max_retries=1,
        allows_network=False, allows_gpu=False,
        description="Room to read a lot and no reason to branch or reach out.",
    ),
}


# ── collaboration (§12) ────────────────────────────────────────────────────

COLLABORATION_PROFILES: Dict[str, CollaborationProfile] = {
    "default": CollaborationProfile(
        id="default", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="on_assignment", write_policy="scoped_write",
        review_own_work=True, preferred_roles=("worker",),
        handoff_contract="",
        description="Works when told, inside its scope, reviews itself. No handoff shape.",
    ),
    "solo_worker_v1": CollaborationProfile(
        id="solo_worker_v1", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="on_assignment", write_policy="scoped_write",
        review_own_work=True, preferred_roles=("worker", "implementer"),
        handoff_contract="implementation_result_v1",
        description="One agent, one task, no room to coordinate with.",
    ),
    "driver_v1": CollaborationProfile(
        id="driver_v1", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="free", write_policy="scoped_write",
        review_own_work=False, preferred_roles=("implementer", "driver"),
        handoff_contract="implementation_result_v1",
        description="Holds the keyboard in a pair; someone else checks the work.",
    ),
    "navigator_v1": CollaborationProfile(
        id="navigator_v1", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="free", write_policy="read_only",
        review_own_work=False, preferred_roles=("navigator", "critic"),
        handoff_contract="review_findings_v1",
        description="Watches the driver, says things, changes nothing.",
    ),
    "independent_reviewer_v1": CollaborationProfile(
        id="independent_reviewer_v1", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="on_assignment_or_blocking_finding", write_policy="read_only",
        review_own_work=False, preferred_roles=("reviewer", "critic"),
        handoff_contract="review_findings_v1",
        description="Reviews other people's work, never its own, never fixes it.",
    ),
    "team_coordinator_v1": CollaborationProfile(
        id="team_coordinator_v1", version="v1",
        may_lead=True, may_delegate=True,
        speak_policy="free", write_policy="propose_only",
        review_own_work=False, preferred_roles=("coordinator", "planner"),
        handoff_contract="implementation_result_v1",
        description="Splits and sequences the work; the workers do the writing.",
    ),
    "exploration_council_v1": CollaborationProfile(
        id="exploration_council_v1", version="v1",
        may_lead=True, may_delegate=True,
        speak_policy="free", write_policy="read_only",
        review_own_work=False,
        preferred_roles=("explorer", "researcher", "critic"),
        handoff_contract="research_report_v1",
        description="Council and Branching: hypotheses compared, no mutating effects.",
    ),
    "creative_pipeline_v1": CollaborationProfile(
        id="creative_pipeline_v1", version="v1",
        may_lead=True, may_delegate=True,
        speak_policy="on_checkpoint", write_policy="scoped_write",
        review_own_work=False,
        preferred_roles=("creative_director", "image_artist", "video_editor"),
        handoff_contract="implementation_result_v1",
        description="Stages that hand artefacts down the line and report at each gate.",
    ),
    "resumable_background_v1": CollaborationProfile(
        id="resumable_background_v1", version="v1",
        may_lead=False, may_delegate=False,
        speak_policy="on_checkpoint", write_policy="scoped_write",
        review_own_work=True, preferred_roles=("worker",),
        handoff_contract="implementation_result_v1",
        description="Long unattended work: checkpoints, resumes, publishes nothing on its own.",
    ),
}


# ── output contracts (§13) ─────────────────────────────────────────────────

OUTPUT_CONTRACTS: Dict[str, OutputContract] = {
    "review_findings_v1": OutputContract(
        id="review_findings_v1", version="v1",
        fields=("findings", "summary", "blocking_count", "unknowns"),
        required=("findings", "summary", "blocking_count"),
        description="What a reviewer hands back: findings with evidence, and what it could not tell.",
    ),
    "implementation_result_v1": OutputContract(
        id="implementation_result_v1", version="v1",
        fields=("changeset", "tests", "proof", "decisions", "known_limits", "followups"),
        required=("changeset", "tests", "proof"),
        description="What a builder hands back: the change, its tests and the proof they ran.",
    ),
    "research_report_v1": OutputContract(
        id="research_report_v1", version="v1",
        fields=("answer", "claims", "sources", "disagreements", "unknowns", "next_checks"),
        required=("answer", "claims", "sources"),
        description="What a researcher hands back: claims tied to sources, disagreements kept.",
    ),
}


# ── the registry ───────────────────────────────────────────────────────────

_REGISTRIES: Dict[str, Dict[str, Any]] = {
    "context": CONTEXT_PROFILES,
    "verification": VERIFICATION_PROFILES,
    "budget": BUDGET_PROFILES,
    "collaboration": COLLABORATION_PROFILES,
    "output": OUTPUT_CONTRACTS,
}

_TYPES: Dict[str, type] = {
    "context": ContextProfileRef,
    "verification": VerificationProfile,
    "budget": BudgetProfile,
    "collaboration": CollaborationProfile,
    "output": OutputContract,
}


def _kind(kind: str) -> str:
    key = str(kind or "").strip()
    if key not in _REGISTRIES:
        raise ProfileError(
            "agent_profiles.kind",
            "unknown profile kind; known kinds are " + ", ".join(KINDS),
            got=kind,
        )
    return key


def get(kind: str, profile_id: str) -> Optional[Any]:
    """The profile, or `None`. Use `require` when absence is a bug."""
    return _REGISTRIES[_kind(kind)].get(str(profile_id or "").strip())


def exists(kind: str, profile_id: str) -> bool:
    return get(kind, profile_id) is not None


def require(kind: str, profile_id: str) -> Any:
    """The profile, or a rejection that lists what is actually there.

    Naming the available ids is not decoration: the caller is usually a
    definition with a typo in its frontmatter, and "unknown verification
    profile" without the list turns a ten-second fix into a grep.
    """
    key = _kind(kind)
    found = get(key, profile_id)
    if found is None:
        available = ", ".join(sorted(_REGISTRIES[key])) or "(none registered)"
        raise ProfileError(
            "agent_profiles." + key,
            "no such {} profile; available: {}".format(key, available),
            got=profile_id,
        )
    return found


def list_profiles(kind: str) -> List[Dict[str, Any]]:
    """Every profile of one kind as plain dicts, sorted by id, for the API."""
    registry = _REGISTRIES[_kind(kind)]
    return [
        {f.name: getattr(profile, f.name) for f in dataclass_fields(profile)}
        for _, profile in sorted(registry.items())
    ]


def _validate(kind: str, profile: Any) -> None:
    """Reject a profile that cannot mean what it says.

    Three checks, each of them a bug that would otherwise surface as strange
    behaviour much later: a blocking check nobody declared, a required output
    field that is not in the contract, and a collaboration policy spelled in a
    vocabulary the strictness ordering does not know.
    """
    expected = _TYPES[kind]
    if not isinstance(profile, expected):
        raise ProfileError(
            "agent_profiles." + kind,
            "expected a {}".format(expected.__name__),
            got=type(profile).__name__,
        )
    if not str(getattr(profile, "id", "") or "").strip():
        raise ProfileError("agent_profiles." + kind, "profile id must not be empty")
    if kind == "verification":
        stray = tuple(c for c in profile.blocking if c not in profile.checks)
        if stray:
            raise ProfileError(
                "agent_profiles.verification." + profile.id,
                "blocking checks must be declared in checks; {} are not".format(list(stray)),
            )
    elif kind == "output":
        stray = tuple(f for f in profile.required if f not in profile.fields)
        if stray:
            raise ProfileError(
                "agent_profiles.output." + profile.id,
                "required fields must be declared in fields; {} are not".format(list(stray)),
            )
    elif kind == "collaboration":
        if profile.write_policy not in WRITE_POLICIES:
            raise ProfileError(
                "agent_profiles.collaboration." + profile.id,
                "unknown write_policy; known are " + ", ".join(WRITE_POLICIES),
                got=profile.write_policy,
            )
        if profile.speak_policy not in SPEAK_POLICIES:
            raise ProfileError(
                "agent_profiles.collaboration." + profile.id,
                "unknown speak_policy; known are " + ", ".join(SPEAK_POLICIES),
                got=profile.speak_policy,
            )
    elif kind == "context":
        if not str(profile.engine_profile_id or "").strip():
            raise ProfileError(
                "agent_profiles.context." + profile.id,
                "engine_profile_id must name a Context Engine profile",
            )
        tokens = profile.budget_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ProfileError(
                "agent_profiles.context." + profile.id,
                "budget_tokens must be a positive integer", got=tokens,
            )


def register(kind: str, profile: Any) -> None:
    """Add a profile — from an agent pack, a plugin, or a test.

    Re-registering the identical profile is a no-op, because packs load more
    than once and failing on that would be noise. Re-registering the same id
    with *different* content is rejected: two runs whose receipts both say
    `full_delivery_v1` must have been verified the same way, and silently
    letting the last loader win is how that stops being true.
    """
    key = _kind(kind)
    _validate(key, profile)
    registry = _REGISTRIES[key]
    existing = registry.get(profile.id)
    if existing is not None:
        if existing == profile:
            logger.debug("%s profile %r re-registered unchanged", key, profile.id)
            return
        raise ProfileError(
            "agent_profiles." + key + "." + profile.id,
            "already registered with different content; ids are versioned so "
            "a change needs a new id, not a redefinition",
        )
    registry[profile.id] = profile
    logger.info("registered %s profile %r", key, profile.id)


# ── strictness ─────────────────────────────────────────────────────────────
#
# One key function per kind, each returning a tuple where *larger is stricter*,
# compared lexicographically. This is not `max`: for verification, more is
# stricter, and for a budget, less is. Writing the two orderings separately is
# the whole point — a single generic comparison would silently get one of them
# backwards, and the direction it got wrong would be the one that hands a
# reviewer a bigger budget than the task asked for.

def _verification_strictness(p: VerificationProfile) -> Tuple[Any, ...]:
    # Blocking checks decide first: a check that can stop the run is worth more
    # than one that only writes a note. Ties fall through to total checks, then
    # to whether a human reviewer is owed.
    return (len(p.blocking), len(p.checks), int(p.requires_reviewer))


def _context_strictness(p: ContextProfileRef) -> Tuple[Any, ...]:
    # A narrower context is the stricter request: fewer tokens, less room to
    # wander. The engine profile itself is not ranked here; the engine owns it.
    return (-int(p.budget_tokens),)


def _budget_strictness(p: BudgetProfile) -> Tuple[Any, ...]:
    # Less of everything is stricter, so every term is negated. Tokens decide
    # first because they are the dimension every backend actually enforces;
    # the rest break ties in the order a run tends to hit them.
    return (
        -int(p.max_tokens), -int(p.max_seconds), -int(p.max_rounds),
        -int(p.max_tool_calls), -int(p.max_branches), -int(p.max_retries),
        -int(p.allows_network), -int(p.allows_gpu),
    )


def _collaboration_strictness(p: CollaborationProfile) -> Tuple[Any, ...]:
    # Fewer liberties is stricter: cannot lead, cannot delegate, writes less,
    # speaks less, and does not review its own work. The last one is not
    # obvious and is deliberate — independence is a constraint, not a freedom.
    return (
        int(not p.may_lead),
        int(not p.may_delegate),
        -WRITE_POLICIES.index(p.write_policy),
        -SPEAK_POLICIES.index(p.speak_policy),
        int(not p.review_own_work),
    )


def _output_strictness(p: OutputContract) -> Tuple[Any, ...]:
    # A contract that demands more required fields is harder to satisfy. Ties
    # go to the one with fewer optional extras: between two contracts that both
    # insist on three fields, the one that tolerates less around them is the
    # tighter shape to hand to the next agent.
    return (len(p.required), -(len(p.fields) - len(p.required)))


_STRICTNESS: Dict[str, Callable[[Any], Tuple[Any, ...]]] = {
    "verification": _verification_strictness,
    "context": _context_strictness,
    "budget": _budget_strictness,
    "collaboration": _collaboration_strictness,
    "output": _output_strictness,
}


def stricter(kind: str, a: str, b: str) -> str:
    """The stricter of two profile ids of the same kind.

    A tie returns `a`. That is not a coin flip: `a` is the incumbent in every
    caller — the definition's own profile, against which a task override is
    being weighed — and §15 says an override may choose verification that is
    *equal or stronger*, so an equal one changes nothing.
    """
    key = _kind(kind)
    left = require(key, a)
    right = require(key, b)
    rank = _STRICTNESS[key]
    return right.id if rank(right) > rank(left) else left.id


# ── reduction ──────────────────────────────────────────────────────────────

#: The numeric ceilings a patch may lower.
_BUDGET_LIMITS: Tuple[str, ...] = (
    "max_tokens", "max_seconds", "max_rounds",
    "max_tool_calls", "max_branches", "max_retries",
)
#: The switches a patch may turn off, and only off.
_BUDGET_SWITCHES: Tuple[str, ...] = ("allows_network", "allows_gpu")


def reduce_budget(base: BudgetProfile, patch: Mapping[str, Any]) -> BudgetProfile:
    """Apply the parts of `patch` that shrink `base`, and report the rest.

    §11 and §15: a run may reduce its own budget freely, and raising it needs
    authority this function does not have. So an over-reaching patch is neither
    an exception nor a grant — each field it failed to move comes back named in
    `BudgetProfile.ignored`, where a run receipt will show it. Refusing loudly
    but returning something usable is the behaviour that keeps a task with one
    optimistic field from failing outright, without ever handing it the field.

    Unknown keys are reported the same way rather than dropped: §22 is explicit
    that an unrecognised field is a typo somebody needs to see, not a default.

    The result carries a derived id when anything actually changed, because a
    reduced budget is a run-scoped value and a receipt that called it
    `standard_v1` would be claiming a catalogue entry that says other numbers.
    The suffix is `.reduced` rather than something more decorative so the
    derived id still satisfies the id grammar in `contracts` and can travel
    into a record instead of being rejected at the far end.
    A patch that changes nothing returns `base` itself.
    """
    if not isinstance(base, BudgetProfile):
        raise ProfileError("reduce_budget.base", "expected a BudgetProfile",
                           got=type(base).__name__)
    if patch is None:
        return base
    if not isinstance(patch, Mapping):
        raise ProfileError("reduce_budget.patch", "expected a mapping",
                           got=type(patch).__name__)

    changes: Dict[str, Any] = {}
    ignored: List[str] = []

    # Sorted so the ignored list reads the same way every time; a receipt that
    # reorders itself between runs is a diff nobody can use.
    for name, value in sorted(((str(k), v) for k, v in patch.items()),
                              key=lambda pair: pair[0]):
        if name in _BUDGET_LIMITS:
            current = getattr(base, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ignored.append("{}: expected a number, got {}".format(
                    name, type(value).__name__))
            elif value != value or value < 0:  # NaN or negative
                ignored.append("{}: {!r} is not a limit".format(name, value))
            elif value >= current:
                ignored.append("{}: {} does not reduce {}".format(name, value, current))
            else:
                changes[name] = int(value)
        elif name in _BUDGET_SWITCHES:
            if value is not False:
                ignored.append("{}: a reduction can switch this off, never on".format(name))
            elif getattr(base, name) is False:
                ignored.append("{}: already off".format(name))
            else:
                changes[name] = False
        else:
            ignored.append("{}: not a budget field".format(name))

    if not changes and not ignored:
        return base
    if ignored:
        logger.warning("budget patch on %r granted nothing for: %s",
                       base.id, "; ".join(ignored))
    if not changes:
        return replace(base, ignored=tuple(ignored))
    return replace(
        base,
        id=base.id + ".reduced",
        ignored=tuple(ignored),
        **changes,
    )
