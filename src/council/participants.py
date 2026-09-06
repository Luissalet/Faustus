"""
council/participants.py — who is in the room, and what each of them may do.

The failure this file exists to prevent has a shape everybody recognises: a
model writes "I'll just fix it myself and run the tests", and something
downstream believes it.  Group chat had no answer to that, because a
participant there was a model name plus a system prompt — so "reviewer" was an
adjective and never a permission, and the only thing standing between a polite
sentence and a write was the hope that the model meant it.

§25 states the rule this module implements: *permisos calculados en el
servidor*.  A participant never declares its own tool profile, and nothing
here can widen what the user and the project already authorised.

Four consequences, one function each:

* `effective_profile` only ever REDUCES.  It takes the floor of what the spec
  asked for, what the participant's roles justify (`role_default_profile`, §5)
  and what the room's policy consents to (§4.1: chat grants no mutating
  tools).  There is no branch in it that raises a profile.
* `resolve_participants` never turns a `ResolvedAgentExecution` into more than
  it granted.  When the profile cannot be resolved at all the participant
  lands on `read_only` with a `degraded` line that says so — never on a
  generous default, because a default nobody chose is the permission nobody
  audits.
* `check_role_conflicts` names the arrangement §5 forbids outright: the same
  participant implementing and being the only judge of its own work, whenever
  the room holds somebody else who could judge instead.
* `rotation_order` is a seeded permutation, because §22 lists "un modelo domina
  por orden" as a real risk and insertion order is exactly that risk.

Nothing here calls a model.  `suggest_roles` is a table and a walk over it, so
that opening a room costs no round trip and two identical rooms open
identically (§5: the automatic assignment must explain itself and be editable
before anything with effects runs).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.council.contracts import (
    ROLES,
    TOOL_PROFILES,
    CouncilError,
    CouncilParticipant,
    can_write,
    new_id,
    role_default_profile,
)

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_TOOL_CEILINGS",
    "UNKNOWN_POLICY_CEILING",
    "IMPLEMENTING_ROLES",
    "JUDGING_ROLES",
    "ParticipantSpec",
    "ResolvedParticipant",
    "participant_id",
    "participant_ids",
    "policy_ceiling",
    "effective_profile",
    "can_participant_write",
    "resolve_participants",
    "suggest_roles",
    "explain_roles",
    "check_role_conflicts",
    "rotation_order",
    "describe",
]


# ── what a room of each kind consents to ───────────────────────────────────
#
# A ceiling, never a grant.  `collaborate` and `pair` sit at the top of
# `TOOL_PROFILES` because those two rooms exist to change something (§4.4,
# §4.5) — which means only that they do not reduce anything on their own, not
# that they hand anybody `full_with_gates`.  The talking rooms are capped at
# `read_only` because §4.1 says so in one line: "no se conceden herramientas
# mutantes salvo designación explícita de ejecutor", and a `tournament` is a
# blind competition of answers, not of writes.

POLICY_TOOL_CEILINGS: Dict[str, str] = {
    "chat": "read_only",
    "consult": "read_only",
    "debate": "read_only",
    "collaborate": "full_with_gates",
    "pair": "full_with_gates",
    "tournament": "read_only",
}

#: A policy this file has never heard of gets nothing.  Same reason
#: `role_default_profile` answers `none` for an unknown role: a room whose
#: rules we cannot read is not a room we can price permissions in, and
#: "probably read_only" is a guess wearing a permission's clothes.
UNKNOWN_POLICY_CEILING: str = "none"

#: The roles that change something.  §5's table, read as two columns.
IMPLEMENTING_ROLES: Tuple[str, ...] = ("driver", "integrator")

#: The roles that judge somebody's work.  `tester` is deliberately absent: it
#: designs and runs tests inside its own scope, which is evidence rather than a
#: verdict, and §5's prohibition is about being the *judge*.
JUDGING_ROLES: Tuple[str, ...] = ("critic", "reviewer", "judge")

_ID_UNSAFE = re.compile(r"[^a-z0-9]+")
_MAX_ROOM = 64


# ── the two shapes ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParticipantSpec:
    """What a caller asks for: a seat, not a permission.

    Everything here is a REQUEST.  `tool_profile` in particular is the most a
    spec may ask for and never what it gets — `effective_profile` decides that
    — and `roles` may be left empty for `suggest_roles` to fill in.
    """

    display_name: str = ""
    model: str = ""
    endpoint_id: str = ""
    agent_slug: str = ""
    roles: Tuple[str, ...] = ()
    kind: str = "model"
    tool_profile: str = ""
    completion_mode: str = ""


@dataclass(frozen=True)
class ResolvedParticipant:
    """One seat, filled: the contract object, its resolution, and the truth.

    `caveats` are things a reader must know that changed nothing — a
    quarantined model the user asked for by name, a model the caller never
    observed.  `degraded` is the other half: something we wanted and did not
    get, so the seat is narrower than it looks.  Two lists rather than one
    because §1.3's rule holds here too — an absent integration degrades with a
    name, and never to an invented success.
    """

    participant: CouncilParticipant
    resolution: Optional[Any] = None
    caveats: Tuple[str, ...] = ()
    degraded: Tuple[str, ...] = ()


# ── small readers ──────────────────────────────────────────────────────────

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_spec(raw: Any) -> ParticipantSpec:
    """A spec from whatever the caller had: the dataclass, or a mapping.

    A key this build does not declare is dropped with a debug line rather than
    raising.  A route that grew a field before this module did should open a
    room with the fields that exist, not fail to open one at all.
    """
    if isinstance(raw, ParticipantSpec):
        return raw
    if isinstance(raw, Mapping):
        known = ParticipantSpec.__dataclass_fields__  # noqa: SLF001 - our own class
        extra = sorted(set(raw) - set(known))
        if extra:
            logger.debug("council participants: ParticipantSpec does not declare %s; dropped",
                         extra)
        data = {k: v for k, v in raw.items() if k in known}
        roles = data.get("roles")
        if roles is not None:
            data["roles"] = tuple(_text(r) for r in roles if _text(r))
        return ParticipantSpec(**data)
    logger.debug("council participants: %r is not a participant spec; ignored", type(raw))
    return ParticipantSpec()


def _unwrap(entry: Any) -> Any:
    """The `CouncilParticipant` inside whatever was handed over.

    Callers hold `ResolvedParticipant`s and pass them straight to
    `check_role_conflicts` and `rotation_order`; making them unwrap first would
    be a rule nobody remembers on the one call site that matters.
    """
    inner = getattr(entry, "participant", None)
    return inner if inner is not None else entry


def _roles_of(entry: Any) -> Tuple[str, ...]:
    return tuple(_text(r) for r in (getattr(_unwrap(entry), "roles", ()) or ()) if _text(r))


def _id_of(entry: Any) -> str:
    if isinstance(entry, str):
        return _text(entry)
    return _text(getattr(_unwrap(entry), "id", ""))


def _rank(profile: str) -> int:
    """Where a profile sits on `TOOL_PROFILES`, least dangerous first.

    An unknown name reads as `none` (rank 0) rather than raising: this is used
    to take a floor, and a typo must make the answer smaller, never larger.
    """
    name = _text(profile)
    try:
        return TOOL_PROFILES.index(name)
    except ValueError:
        if name:
            logger.warning("council participants: unknown tool profile %r; read as 'none'", name)
        return 0


def _floor(*profiles: str) -> str:
    """The least permissive of the profiles offered."""
    if not profiles:
        return "none"
    return TOOL_PROFILES[min(_rank(p) for p in profiles)]


# ── naming a seat ──────────────────────────────────────────────────────────

def participant_id(spec: Any, *, taken: Sequence[str] = ()) -> str:
    """A stable, readable id for one spec: `p_claude`, `p_qwen3_coder`.

    Prefixed because §16 refuses a participant called `user`, `system`, `tool`
    or `room` — those are author kinds and audience tokens, and a filter that
    meets one as a participant id cannot tell the two apart afterwards.  The
    prefix makes the collision impossible instead of catching it later, which
    is why the user's own seat is `p_user`.

    Derived from the display name (then the agent slug, then the model) so that
    reopening the same room twice produces the same ids and a stored rotation
    order still means something.
    """
    entry = _as_spec(spec)
    base = (_text(entry.display_name) or _text(entry.agent_slug)
            or _text(entry.model) or "participant")
    slug = _ID_UNSAFE.sub("_", base.lower()).strip("_") or "participant"
    candidate = f"p_{slug}"[:120]
    if candidate not in set(taken):
        return candidate
    for suffix in range(2, _MAX_ROOM + 2):
        numbered = f"{candidate}_{suffix}"
        if numbered not in set(taken):
            return numbered
    return new_id("p")


def participant_ids(specs: Sequence[Any]) -> List[str]:
    """The ids of a whole room, in the caller's order, none of them repeated."""
    out: List[str] = []
    for spec in specs or ():
        out.append(participant_id(spec, taken=out))
    return out


# ── the profile, computed here and nowhere else (§25) ──────────────────────

def policy_ceiling(policy: str) -> str:
    """The most a room of this kind consents to, whatever anyone asks for."""
    name = _text(policy)
    if name in POLICY_TOOL_CEILINGS:
        return POLICY_TOOL_CEILINGS[name]
    logger.warning("council participants: unknown session policy %r; capping at %r",
                   name, UNKNOWN_POLICY_CEILING)
    return UNKNOWN_POLICY_CEILING


def effective_profile(participant: Any, *, session_policy: str) -> str:
    """The tool profile a participant actually holds.

    The floor of three numbers and nothing else:

    * what the participant carries (already the most its spec asked for),
    * what its roles justify — `CouncilParticipant.suggested_profile()`, which
      is `role_default_profile` over its roles; a participant wearing two hats
      is measured by the more permissive one, because §5 allows two hats when
      models are scarce and a driver who is also a critic still has to drive,
    * what the room's policy consents to.

    It cannot raise a profile.  That is not a property of the current code that
    a later edit might lose by accident: there is no argument to this function
    that names a bigger profile than the participant already carries, so the
    only reachable answers are the ones at or below it.
    """
    held = _text(getattr(_unwrap(participant), "tool_profile", "")) or "none"
    try:
        by_role = _text(_unwrap(participant).suggested_profile())
    except Exception:  # noqa: BLE001 - a participant-shaped mapping has no method
        by_role = _best_role_profile(_roles_of(participant))
    return _floor(held, by_role, policy_ceiling(session_policy))


def _best_role_profile(roles: Sequence[str]) -> str:
    """The most permissive profile a set of roles justifies.

    The same walk `CouncilParticipant.suggested_profile()` does, kept here for
    the callers that hold roles and not a contract object yet — a spec being
    resolved has roles and no participant.
    """
    best = "none"
    for role in roles or ():
        candidate = role_default_profile(role)
        if _rank(candidate) > _rank(best):
            best = candidate
    return best


def can_participant_write(participant: Any) -> bool:
    """Whether this participant may modify anything at all, right now.

    Asks `contracts.can_write` about the profile it HOLDS, never about the
    profile its roles would suggest: a reviewer the user explicitly authorised
    to correct writes, and a driver the room narrowed does not.  One
    implementation of "may write", so that two callers cannot disagree.
    """
    return can_write(_text(getattr(_unwrap(participant), "tool_profile", "")))


# ── the agent profile, and what it is allowed to imply ─────────────────────

def _resolve_agent(spec: ParticipantSpec, *, owner: str, session: Any, workspace: str,
                   available_models: Sequence[str], unhealthy: Sequence[str],
                   quarantined: Sequence[str]) -> Tuple[Optional[Any], str]:
    """`resolver.resolve()` for one spec, or `(None, why)`.

    The import is lazy and guarded on purpose.  `src.agent_profiles.resolver`
    reads the definition store; a council that cannot open because that store
    is broken is a worse outcome than a council whose seats are read-only and
    say why.
    """
    slug = _text(spec.agent_slug)
    if not slug:
        return None, ""
    try:
        from src.agent_profiles.resolver import resolve as _resolve
    except Exception as exc:  # noqa: BLE001 - never fatal on the room's path
        return None, ("the agent-profile resolver is not available in this build "
                      f"({type(exc).__name__})")
    try:
        return _resolve(
            agent=slug,
            scope={"owner": owner,
                   "project_id": _text(getattr(session, "project_id", "")),
                   "session_id": _text(getattr(session, "id", ""))},
            available_models=tuple(available_models or ()),
            unhealthy=tuple(unhealthy or ()),
            quarantined=tuple(quarantined or ()),
            workspace=workspace or None,
        ), ""
    except Exception as exc:  # noqa: BLE001 - resolve() promises not to raise
        logger.exception("council participants: resolving %r failed", slug)
        return None, f"resolving the agent profile {slug!r} failed ({type(exc).__name__})"


def _resolution_ceiling(resolution: Any) -> Tuple[str, str, bool]:
    """`(ceiling, why, is_degradation)` — what the resolution allows at most.

    Three answers, and the difference between the last two is the whole of
    rule 3:

    * no resolution was asked for → `("", "", False)`: no opinion.  A
      participant without an `agent_slug` is a model and nothing more (§7.2),
      which is a legitimate seat, not a failure.
    * an envelope that grants nothing at all — no tool, no work root, no effect
      — is what `resolver._minimal()` returns when resolution FAILED.  The seat
      is capped at `read_only` and the reason is a DEGRADATION, because we
      wanted a profile and did not get one.
    * an envelope with tools but no work root cannot honour any profile that
      writes, so the cap is `review` and the reason is a caveat: that is a
      configuration somebody chose, not something that broke.

    In no branch does this function return something larger than what the
    caller already had; it is only ever passed to `_floor`.
    """
    if resolution is None:
        return "", "", False
    permissions = getattr(resolution, "permissions", None)
    tools = tuple(getattr(permissions, "tools", ()) or ())
    roots = tuple(getattr(permissions, "work_roots", ()) or ())
    effects = tuple(getattr(permissions, "effects", ()) or ())
    if not tools and not roots and not effects:
        return ("read_only",
                "the resolved profile grants no tool, work root or effect; this seat holds "
                "the reading rights of the room and nothing else", True)
    if not roots:
        return ("review",
                "the resolved profile grants no work root, so no profile that writes can be "
                "honoured for this seat", False)
    return "", "", False


def resolve_participants(specs: Sequence[Any], *, session: Any, owner: str = "",
                         workspace: str = "", available_models: Sequence[str] = (),
                         unhealthy: Sequence[str] = (),
                         quarantined: Sequence[str] = ()) -> List[ResolvedParticipant]:
    """Fill every seat in the room, and say what each one cost.

    The four availability lists are what the CALLER observed — the same choice
    `resolver.resolve()` makes, for the same reason: there is no Immune System
    to ask from here, and an absent one must degrade with a name rather than to
    an invented `healthy` (§1.3).

    Quarantine excludes rather than being ignored (§1.9.6).  A quarantined
    model that arrived from an automatic resolution takes the seat with a
    `degraded` line and the profile `none`: a capability in quarantine does not
    get tools because a room happened to want one.  A quarantined model the
    user NAMED keeps its profile and gets a caveat — the user is allowed to
    take a risk they can see, and hiding their own choice from them is not
    safety.

    Never raises.  A spec the contracts refuse becomes a seat with no roles, no
    profile and a `degraded` line naming the rejection: a room that cannot be
    opened at all is the failure with the fewest options for the person in it.
    """
    room = list(specs or ())
    ids = participant_ids(room)
    policy = _text(getattr(session, "policy", "")) or "chat"
    effective_owner = _text(owner) or _text(getattr(session, "owner", ""))
    effective_workspace = _text(workspace) or _text(getattr(session, "workspace", ""))
    quarantine = {_text(m) for m in (quarantined or ()) if _text(m)}
    sick = {_text(m) for m in (unhealthy or ()) if _text(m)}
    observed = {_text(m) for m in (available_models or ()) if _text(m)}

    out: List[ResolvedParticipant] = []
    for index, raw in enumerate(room):
        spec = _as_spec(raw)
        out.append(_resolve_one(
            spec, participant=ids[index], session=session, policy=policy,
            owner=effective_owner, workspace=effective_workspace,
            available_models=tuple(sorted(observed)), observed=observed,
            unhealthy=sick, quarantined=quarantine))
    return out


def _resolve_one(spec: ParticipantSpec, *, participant: str, session: Any, policy: str,
                 owner: str, workspace: str, available_models: Sequence[str],
                 observed: set, unhealthy: set, quarantined: set) -> ResolvedParticipant:
    """One seat.  Split out of `resolve_participants` so that the loop reads as
    a loop and the rules read as rules."""
    caveats: List[str] = []
    degraded: List[str] = []

    resolution, why = _resolve_agent(
        spec, owner=owner, session=session, workspace=workspace,
        available_models=available_models, unhealthy=unhealthy,
        quarantined=quarantined)
    if why:
        degraded.append(f"agent_profile: {why}")
    for line in (getattr(resolution, "caveats", ()) or ()):
        caveats.append(f"profile: {_text(line)}")
    for line in (getattr(resolution, "degraded_integrations", ()) or ()):
        degraded.append(_text(line))

    route = getattr(resolution, "model_route", None)
    asked_model = _text(spec.model)
    model = asked_model or _text(getattr(route, "model", ""))
    endpoint = _text(spec.endpoint_id) or _text(getattr(route, "endpoint_id", ""))
    completion = (_text(spec.completion_mode)
                  or _text(getattr(getattr(resolution, "completion", None), "mode", "")))

    # Health and quarantine (§1.9.6).  Both are the caller's observation, and
    # both are recorded rather than acted on silently.
    status = "available"
    named_it = bool(asked_model) and asked_model == model
    if model and model in quarantined:
        if named_it:
            caveats.append(f"model {model!r} is quarantined; it is in this room because it was "
                           f"named explicitly")
        else:
            status = "quarantined"
            degraded.append(f"quarantine: {model!r} is quarantined and was not selected "
                            f"automatically for this seat")
    elif model and model in unhealthy:
        status = "unhealthy"
        caveats.append(f"model {model!r} was reported unhealthy by the caller; the room may "
                       f"still seat it, but it should expect failures")
    if model and observed and model not in observed:
        caveats.append(f"model {model!r} was not among the {len(observed)} model(s) the caller "
                       f"observed; nothing here proved it is reachable")
    if not model:
        degraded.append("model: this seat has no model; a spec that names neither a model nor a "
                        "resolvable agent cannot be routed")

    # The profile, floor by floor.  Every reduction that actually bites is
    # recorded, because "why can this seat not write?" has to be answerable
    # from the room and not from this source file.
    roles = tuple(_text(r) for r in (spec.roles or ()) if _text(r))
    asked = _text(spec.tool_profile) or _best_role_profile(roles)
    by_role = _best_role_profile(roles)
    by_policy = policy_ceiling(policy)
    if _text(spec.agent_slug) and resolution is None:
        # A profile was asked for and could not be produced.  Rule 3: the seat
        # falls to `read_only` and says why — never to the profile its role
        # would have justified, which would be permissions granted on the
        # strength of a resolution that never happened.
        by_resolution, resolution_why, resolution_degrades = (
            "read_only",
            "the agent profile could not be resolved, so this seat holds the reading rights "
            "of the room and nothing else", True)
    else:
        by_resolution, resolution_why, resolution_degrades = _resolution_ceiling(resolution)
    profile = _floor(*(p for p in (asked, by_role, by_policy, by_resolution) if p))
    if status == "quarantined":
        profile = "none"
    if _rank(by_role) < _rank(asked):
        caveats.append(f"tool profile: {asked!r} was asked for; the role(s) "
                       f"{list(roles) or ['(none)']} justify at most {by_role!r}")
    if _rank(by_policy) < _rank(_floor(asked, by_role)):
        caveats.append(f"tool profile: a {policy!r} room consents to at most {by_policy!r}")
    if by_resolution and _rank(by_resolution) < _rank(_floor(asked, by_role, by_policy)):
        (degraded if resolution_degrades else caveats).append(
            f"tool profile: {resolution_why}")

    try:
        contract = CouncilParticipant.parse({
            "id": participant,
            "display_name": _text(spec.display_name) or model or participant,
            "kind": _text(spec.kind) or "model",
            "model": model,
            "endpoint_id": endpoint,
            "agent_slug": _text(spec.agent_slug),
            "roles": list(roles),
            "tool_profile": profile,
            "status": status,
            "capabilities": [],
            "resolution_id": _text(getattr(resolution, "resolution_id", "")),
            "completion_mode": completion,
        })
    except CouncilError as exc:
        logger.warning("council participants: %s was refused by the contracts (%s)",
                       participant, exc)
        degraded.append(f"contract: this seat was refused as specified ({exc}); it holds no "
                        f"role and no tool profile")
        contract = CouncilParticipant(id=participant, display_name=participant,
                                      kind="model", model=model, endpoint_id=endpoint,
                                      tool_profile="none", status=status)
    return ResolvedParticipant(participant=contract, resolution=resolution,
                               caveats=tuple(dict.fromkeys(caveats)),
                               degraded=tuple(dict.fromkeys(degraded)))


# ── suggesting roles, without asking a model (§5) ──────────────────────────
#
# `(seats, tail)`: the roles for the first N positions, then what everybody
# after them gets.  A table rather than a prompt because §5 requires the
# automatic assignment to explain itself and be editable before anything with
# effects runs — and an explanation a model wrote is one nobody can diff.

_ROLE_PLANS: Dict[str, Tuple[Tuple[Tuple[str, ...], ...], Tuple[str, ...]]] = {
    # §4.1 — a moderated group: one seat keeps the agenda, the rest are voices.
    "chat": ((("coordinator",),), ("researcher",)),
    # §4.2 — everyone answers the same question without seeing the others.
    "consult": ((("coordinator", "researcher"),), ("researcher",)),
    # §4.3 — proposal, critique, verdict.
    "debate": ((("architect",), ("critic",), ("judge",)), ("critic",)),
    # §4.4 — an execution team: plan, build, review, test, integrate.
    "collaborate": ((("coordinator",), ("driver",), ("reviewer",), ("tester",),
                     ("integrator",)), ("driver",)),
    # §4.5 — one writer, the rest navigate.
    "pair": ((("driver",), ("reviewer",)), ("reviewer",)),
    # §4.6 — blind answers, then a judge.
    "tournament": ((("researcher",),), ("researcher",)),
}

#: A hint may add ONE role that the plan did not cover.  Substring matching in
#: both languages the project is written in, because the hint is whatever the
#: user typed and half of Faustus' users type Spanish.
_HINT_ROLES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("test", "prueba", "pytest", "regres", "cobertura"), "tester"),
    (("review", "revis", "audit"), "reviewer"),
    (("design", "arquitect", "architect", "contrato", "contract"), "architect"),
    (("research", "investig", "compar", "buscar"), "researcher"),
)


def _clean_roles(roles: Sequence[str]) -> Tuple[str, ...]:
    """Only roles §5 declares, deduplicated, in the order they were offered."""
    out: List[str] = []
    for role in roles or ():
        name = _text(role)
        if name and name in ROLES and name not in out:
            out.append(name)
        elif name and name not in ROLES:
            logger.debug("council participants: %r is not a council role; dropped", name)
    return tuple(out)


def _plan_roles(specs: Sequence[Any], policy: str,
                task_hint: str) -> Dict[str, Tuple[Tuple[str, ...], str]]:
    """`{participant_id: (roles, why)}` — the whole of the suggestion, once.

    Deterministic in every branch: position decides, declared roles win, and
    the hint may only append a role that nobody holds yet, to the LAST seat
    without an implementing role.  Two identical rooms are opened identically,
    which is what makes a suggestion reviewable.
    """
    room = [_as_spec(s) for s in (specs or ())]
    ids = participant_ids(room)
    name = _text(policy)
    seats, tail = _ROLE_PLANS.get(name, _ROLE_PLANS["chat"])
    if name not in _ROLE_PLANS:
        logger.warning("council participants: unknown policy %r; suggesting the chat plan", name)

    plan: Dict[str, Tuple[Tuple[str, ...], str]] = {}
    for index, spec in enumerate(room):
        declared = _clean_roles(spec.roles)
        if declared:
            plan[ids[index]] = (declared, "kept the role(s) the request declared; a suggestion "
                                          "never overrides a choice somebody made")
            continue
        roles = _clean_roles(seats[index] if index < len(seats) else tail)
        plan[ids[index]] = (roles, f"policy {name!r}, seat {index + 1}: "
                                   f"{', '.join(roles) or 'no role'} by the plan of §4")

    # §4.6 — a tournament needs somebody to hold the rubric, and the last seat
    # is the one whose answer nobody else has to critique.
    if name == "tournament" and len(room) >= 3:
        last = ids[-1]
        if not _clean_roles(room[-1].roles):
            plan[last] = (("judge",), "policy 'tournament', last seat: the judge applies the "
                                      "rubric and breaks ties (§4.6)")

    _apply_hint(plan, ids, room, task_hint)
    return plan


def _apply_hint(plan: Dict[str, Tuple[Tuple[str, ...], str]], ids: Sequence[str],
                room: Sequence[ParticipantSpec], task_hint: str) -> None:
    """Add the role the task obviously needs, if the room has nobody doing it.

    Bounded on purpose: one extra role per hint keyword, only on a seat whose
    roles this module chose, and never on a seat that implements — giving the
    driver the reviewer's hat is precisely the arrangement §5 forbids, and
    doing it by accident from a keyword would be worse than not helping.
    """
    hint = _text(task_hint).lower()
    if not hint:
        return
    for keywords, role in _HINT_ROLES:
        if not any(word in hint for word in keywords):
            continue
        if any(role in roles for roles, _ in plan.values()):
            continue
        for index in range(len(room) - 1, -1, -1):
            seat = ids[index]
            roles, why = plan[seat]
            if _clean_roles(room[index].roles):
                continue  # the caller chose this seat's roles; leave them alone
            if any(r in IMPLEMENTING_ROLES for r in roles):
                continue
            plan[seat] = (_clean_roles(tuple(roles) + (role,)),
                          f"{why}; the task hint asked for {role!r} and nobody held it")
            break


def suggest_roles(specs: Sequence[Any], *, policy: str,
                  task_hint: str = "") -> Dict[str, Tuple[str, ...]]:
    """`{participant_id: roles}` for a room nobody has assigned yet.

    A suggestion and not a decision: §5 says the automatic assignment must be
    editable before an execution with effects begins, so this returns roles for
    a caller to show and change, and changes nothing itself.
    """
    return {seat: roles for seat, (roles, _) in _plan_roles(specs, policy, task_hint).items()}


def explain_roles(specs: Sequence[Any], *, policy: str,
                  task_hint: str = "") -> Dict[str, str]:
    """`{participant_id: why}` for the same walk `suggest_roles` just did.

    Separate from `suggest_roles` because the roles are a contract value and
    the reason is a sentence for a person; carrying the sentence inside the
    value would put prose into every payload that passes a role list around.
    """
    return {seat: why for seat, (_, why) in _plan_roles(specs, policy, task_hint).items()}


# ── the arrangement §5 forbids ─────────────────────────────────────────────

def check_role_conflicts(participants: Sequence[Any]) -> List[str]:
    """The seats that implement and judge their own work, when somebody else could.

    §5, in one sentence: *no se debe permitir que el mismo participante sea
    implementador y único juez de su propio trabajo cuando exista una
    alternativa*.  Both halves are load-bearing, and the second one is why this
    returns an empty list for a room of one: a lone model reviewing itself is
    weak, but refusing it would leave the user with nothing at all, and §1.9.10
    says the room may degrade to a single agent.

    Returns readable lines, one per conflict, naming who could judge instead —
    a caller can print them, and `orchestrator` can refuse on them.  It does
    not raise: this is a finding about a configuration, and a finding that
    arrives as an exception is a finding somebody wraps in a `try`.
    """
    rows = [(_id_of(p), _roles_of(p)) for p in (participants or ())]
    rows = [(seat, roles) for seat, roles in rows if seat]
    free = [seat for seat, roles in rows
            if not any(r in IMPLEMENTING_ROLES for r in roles)]

    out: List[str] = []
    for seat, roles in rows:
        implementing = [r for r in IMPLEMENTING_ROLES if r in roles]
        judging = [r for r in JUDGING_ROLES if r in roles]
        if not implementing or not judging:
            continue
        alternatives = [other for other in free if other != seat]
        if not alternatives:
            continue
        out.append(
            f"{seat} implements ({', '.join(implementing)}) and is also a judge of its own work "
            f"({', '.join(judging)}); {', '.join(alternatives)} hold no implementing role and "
            f"could review instead (§5)")
    return out


# ── nobody goes first twice (§22) ──────────────────────────────────────────

def rotation_order(participants: Sequence[Any], *, seed: str) -> List[str]:
    """The order this round speaks in: a permutation of the room, keyed by seed.

    §22 names "un modelo domina por orden o reputación" as a real risk and
    prescribes "rotación determinista/semilla".  Both words matter: the same
    seed always produces the same order — so a turn can be replayed, and a
    recovered session resumes the order it had — and the order is a hash of the
    seat ids rather than the order somebody happened to add them in, so the
    first-listed participant does not speak first every round of every session.

    Ties are broken by insertion order, which only matters if two seats share a
    digest; duplicated ids collapse to their first appearance, because a room
    cannot give the same participant two slots.
    """
    ids: List[str] = []
    for entry in (participants or ()):
        seat = _id_of(entry)
        if seat and seat not in ids:
            ids.append(seat)
    key = _text(seed)

    def _digest(pair: Tuple[int, str]) -> Tuple[str, int]:
        index, seat = pair
        blob = f"{key}\x00{seat}".encode("utf-8", "replace")
        return hashlib.sha256(blob).hexdigest(), index

    return [seat for _, seat in sorted(enumerate(ids), key=_digest)]


# ── saying it out loud ─────────────────────────────────────────────────────

def describe(resolved: ResolvedParticipant) -> Dict[str, Any]:
    """One seat as a row a person or an API can read.

    `resolved` is `False` and `resolution_note` says why in the two cases that
    look alike from outside and are not: a seat that never asked for an agent
    profile is a model and nothing more (§7.2) and that is fine, while a seat
    whose profile could not be resolved is narrower than it looks and says so
    in `degraded`.  Collapsing the two into a null would hide the second.
    """
    seat = _unwrap(resolved)
    resolution = getattr(resolved, "resolution", None)
    slug = _text(getattr(seat, "agent_slug", ""))
    if resolution is not None:
        note = f"resolved from the agent profile {slug!r}" if slug else "resolved"
    elif slug:
        note = (f"the agent profile {slug!r} was requested and could not be resolved; see "
                f"'degraded'")
    else:
        note = ("no agent profile was requested: this participant is a model and nothing more, "
                "which is a seat and not a failure")
    return {
        "id": _text(getattr(seat, "id", "")),
        "display_name": _text(getattr(seat, "display_name", "")),
        "kind": _text(getattr(seat, "kind", "")),
        "model": _text(getattr(seat, "model", "")),
        "endpoint_id": _text(getattr(seat, "endpoint_id", "")),
        "agent_slug": slug,
        "roles": list(_roles_of(seat)),
        "tool_profile": _text(getattr(seat, "tool_profile", "")),
        "may_write": can_participant_write(seat),
        "status": _text(getattr(seat, "status", "")),
        "completion_mode": _text(getattr(seat, "completion_mode", "")),
        "resolution_id": _text(getattr(resolution, "resolution_id", "")),
        "resolved": resolution is not None,
        "resolution_note": note,
        "caveats": list(getattr(resolved, "caveats", ()) or ()),
        "degraded": list(getattr(resolved, "degraded", ()) or ()),
    }
