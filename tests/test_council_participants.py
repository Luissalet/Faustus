"""Tests for src/council/participants.py — the permission rules, not the plumbing.

Every test here is one of the failures the plan names outright: a model that
asks for the write toolset and gets it (§25), a reviewer who is also the
implementer of the thing under review (§5), a quarantined capability seated
because a router happened to like it (§1.9.6), and the same participant
speaking first every round (§22).

The agent-profile resolver is a double.  Not to avoid the real one — it never
raises and would work here — but because these tests are about what
`participants.py` does with a resolution, and a test that has to seed the
definition store to say "an envelope that grants nothing leaves the seat
read-only" is a test nobody keeps green.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

import pytest

from src.council import participants as cp
from src.council.contracts import CouncilParticipant, CouncilSession


# --- doubles ---------------------------------------------------------------

@dataclass
class FakeEnvelope:
    """`PermissionEnvelope`'s three enumerated lists.  Empty grants nothing —
    that is the real class's rule, and the one the ceiling reads."""

    tools: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    work_roots: Tuple[str, ...] = ()
    effects: Tuple[str, ...] = ()


@dataclass
class FakeRoute:
    model: str = ""
    endpoint_id: str = ""


@dataclass
class FakeCompletion:
    mode: str = "greedy"


@dataclass
class FakeResolution:
    resolution_id: str = "res_test"
    permissions: FakeEnvelope = field(default_factory=FakeEnvelope)
    model_route: FakeRoute = field(default_factory=FakeRoute)
    completion: FakeCompletion = field(default_factory=FakeCompletion)
    caveats: Tuple[str, ...] = ()
    degraded_integrations: Tuple[str, ...] = ()


@pytest.fixture
def resolver(monkeypatch):
    """Install a resolver double and hand back a knob to set what it returns."""
    import src.agent_profiles.resolver as real

    box = {"resolution": FakeResolution(
        permissions=FakeEnvelope(tools=("read_file",), work_roots=("D:/repo",)),
        model_route=FakeRoute(model="qwen3-coder", endpoint_id="ep_local"))}

    def _fake(**kw):
        box["kwargs"] = kw
        return box["resolution"]

    monkeypatch.setattr(real, "resolve", _fake)
    return box


@pytest.fixture
def session():
    return CouncilSession.parse({"id": "council_test", "owner": "luis",
                                 "policy": "collaborate", "project_id": "proj"})


def _seat(**fields):
    payload = {"id": "p_x", "kind": "model", "model": "m", "roles": [],
               "tool_profile": "none"}
    payload.update(fields)
    return CouncilParticipant.parse(payload)


# --- rule 1: the server computes the profile, the model never declares it ---

def test_a_critic_asking_for_the_write_toolset_lands_on_read_only():
    """§25 and §5 in one line: asking is not getting.

    `full_with_gates` is the most dangerous profile there is and a critic's
    declared job is to find faults, so the role table caps it at `read_only`
    — even in a `collaborate` room, whose own ceiling would have allowed it.
    """
    seat = _seat(roles=["critic"], tool_profile="full_with_gates")

    assert cp.effective_profile(seat, session_policy="collaborate") == "read_only"
    assert cp.can_participant_write(_seat(roles=["critic"], tool_profile="read_only")) is False


def test_the_same_reduction_happens_through_resolve_participants(session, resolver):
    """The route into this is a spec, so the spec path is tested too."""
    spec = cp.ParticipantSpec(display_name="Claude", model="claude-4",
                              roles=("critic",), tool_profile="full_with_gates")

    seated = cp.resolve_participants([spec], session=session, owner="luis")

    assert len(seated) == 1
    assert seated[0].participant.id == "p_claude"
    assert seated[0].participant.tool_profile == "read_only"
    assert cp.can_participant_write(seated[0]) is False
    assert any("justify at most" in line for line in seated[0].caveats)


def test_a_talking_room_lowers_even_a_driver():
    """§4.1: "no se conceden herramientas mutantes salvo designación explícita
    de ejecutor".  A driver is exactly the role a chat room must not arm."""
    seat = _seat(roles=["driver"], tool_profile="scoped_write")

    assert cp.effective_profile(seat, session_policy="collaborate") == "scoped_write"
    assert cp.effective_profile(seat, session_policy="chat") == "read_only"
    assert cp.effective_profile(seat, session_policy="debate") == "read_only"


def test_an_unknown_policy_grants_nothing():
    """Fail closed.  A room whose rules we cannot read is not a room we can
    price permissions in, and "probably read_only" is a guess in a
    permission's clothes."""
    assert cp.policy_ceiling("brainstorm-2000") == "none"
    assert cp.effective_profile(_seat(roles=["driver"], tool_profile="scoped_write"),
                                session_policy="brainstorm-2000") == "none"


def test_effective_profile_can_never_raise_a_profile():
    """The property, over the whole cross product, not one example of it.

    Every role, every policy, every profile a spec could ask for: the answer is
    never above what the participant already carries, and never above what its
    roles justify.  A later edit that adds a branch which widens something has
    to fail here.
    """
    from src.council.contracts import ROLES, TOOL_PROFILES

    order = {name: index for index, name in enumerate(TOOL_PROFILES)}
    for role in ROLES:
        for policy in cp.POLICY_TOOL_CEILINGS:
            for asked in TOOL_PROFILES:
                seat = _seat(roles=[role], tool_profile=asked)
                got = cp.effective_profile(seat, session_policy=policy)

                assert order[got] <= order[asked]
                assert order[got] <= order[seat.suggested_profile()]
                assert order[got] <= order[cp.policy_ceiling(policy)]


def test_two_hats_are_measured_by_the_more_permissive_one():
    """§5 allows several roles when there are few models; a driver who also
    critiques still has to drive."""
    seat = _seat(roles=["driver", "critic"], tool_profile="scoped_write")

    assert cp.effective_profile(seat, session_policy="collaborate") == "scoped_write"


# --- rule 3: nobody gets permissions from a resolution that granted none ----

def test_a_resolution_that_grants_nothing_leaves_the_seat_read_only(session, resolver):
    """The shape `resolver._minimal()` returns when resolution FAILED: an
    envelope with no tool, no work root and no effect.

    The seat lands on `read_only` and says so in `degraded`.  What it must
    never land on is the profile its role would have justified, because that
    would mean the room handed out a driver's permissions on the strength of a
    resolution that failed."""
    resolver["resolution"] = FakeResolution(permissions=FakeEnvelope(),
                                            model_route=FakeRoute(model="qwen3-coder"))
    spec = cp.ParticipantSpec(display_name="Codex", agent_slug="builder",
                              roles=("driver",), tool_profile="scoped_write")

    seat = cp.resolve_participants([spec], session=session, owner="luis")[0]

    assert seat.participant.tool_profile == "read_only"
    assert cp.can_participant_write(seat) is False
    assert any("grants no tool, work root or effect" in line for line in seat.degraded)
    assert seat.caveats == () or all("grants no tool" not in c for c in seat.caveats)


def test_an_envelope_with_no_work_root_cannot_hold_a_writing_profile(session, resolver):
    """A configuration somebody chose, not something that broke: a caveat, and
    a cap at `review` — the profile that runs tests and writes nothing."""
    resolver["resolution"] = FakeResolution(
        permissions=FakeEnvelope(tools=("read_file", "run_tests")),
        model_route=FakeRoute(model="qwen3-coder"))
    spec = cp.ParticipantSpec(display_name="Codex", agent_slug="builder",
                              roles=("driver",), tool_profile="scoped_write")

    seat = cp.resolve_participants([spec], session=session, owner="luis")[0]

    assert seat.participant.tool_profile == "review"
    assert any("grants no work root" in line for line in seat.caveats)


def test_a_resolver_that_is_unavailable_degrades_and_does_not_raise(session, monkeypatch):
    """A broken definition store must not stop a room from opening."""
    import src.agent_profiles.resolver as real

    def _boom(**kw):
        raise RuntimeError("the definition store is on fire")

    monkeypatch.setattr(real, "resolve", _boom)
    spec = cp.ParticipantSpec(display_name="Codex", agent_slug="builder", model="qwen3-coder",
                              roles=("driver",), tool_profile="scoped_write")

    seat = cp.resolve_participants([spec], session=session, owner="luis")[0]

    assert seat.resolution is None
    # A profile was asked for and never arrived, so the seat is read-only and
    # says why — not the `scoped_write` its role would otherwise have justified.
    assert seat.participant.tool_profile == "read_only"
    assert any("failed" in line for line in seat.degraded)
    assert any("could not be resolved" in line for line in seat.degraded)
    assert cp.describe(seat)["resolved"] is False
    assert "could not be resolved" in cp.describe(seat)["resolution_note"]


# --- rule 4: quarantine and health exclude, they are not ignored (§1.9.6) ---

def test_a_quarantined_model_is_not_seated_automatically(session, resolver):
    """The model came out of a resolution, not out of the user's mouth, so the
    seat is marked and disarmed rather than silently used."""
    resolver["resolution"] = FakeResolution(
        permissions=FakeEnvelope(tools=("read_file",), work_roots=("D:/repo",)),
        model_route=FakeRoute(model="glm-4.6"))
    spec = cp.ParticipantSpec(display_name="Glm", agent_slug="builder", roles=("driver",))

    seat = cp.resolve_participants([spec], session=session, owner="luis",
                                   quarantined=("glm-4.6",))[0]

    assert seat.participant.status == "quarantined"
    assert seat.participant.tool_profile == "none"
    assert cp.can_participant_write(seat) is False
    assert any("was not selected automatically" in line for line in seat.degraded)


def test_a_quarantined_model_the_user_named_is_seated_with_a_caveat(session, resolver):
    """The user is allowed to take a risk they can see.  Hiding their own
    choice from them is not safety."""
    spec = cp.ParticipantSpec(display_name="Glm", model="glm-4.6", roles=("critic",))

    seat = cp.resolve_participants([spec], session=session, owner="luis",
                                   quarantined=("glm-4.6",))[0]

    assert seat.participant.status == "available"
    assert seat.participant.tool_profile == "read_only"
    assert any("named explicitly" in line for line in seat.caveats)
    assert seat.degraded == ()


def test_an_unhealthy_model_is_marked_and_still_seated(session):
    spec = cp.ParticipantSpec(display_name="Codex", model="qwen3-coder", roles=("driver",))

    seat = cp.resolve_participants([spec], session=session, owner="luis",
                                   unhealthy=("qwen3-coder",))[0]

    assert seat.participant.status == "unhealthy"
    assert any("unhealthy" in line for line in seat.caveats)


def test_a_model_the_caller_never_observed_is_named_in_a_caveat(session):
    spec = cp.ParticipantSpec(display_name="Codex", model="qwen3-coder", roles=("critic",))

    seat = cp.resolve_participants([spec], session=session, owner="luis",
                                   available_models=("claude-4", "gpt-5"))[0]

    assert any("was not among the 2 model(s)" in line for line in seat.caveats)


# --- rule 6: no agent slug is not an error ---------------------------------

def test_without_an_agent_slug_a_participant_is_only_a_model(session):
    """§7.2.  A seat that never asked for an agent profile is a model and
    nothing more; `describe()` says that in words rather than leaving a null
    for a reader to interpret as a failure."""
    spec = cp.ParticipantSpec(display_name="Claude", model="claude-4", roles=("critic",))

    seat = cp.resolve_participants([spec], session=session, owner="luis")[0]
    row = cp.describe(seat)

    assert seat.resolution is None
    assert seat.degraded == ()
    assert row["resolved"] is False
    assert "model and nothing more" in row["resolution_note"]
    assert row["tool_profile"] == "read_only"
    assert row["may_write"] is False


# --- rule 2: implementer and only judge of its own work (§5) ---------------

def test_an_implementer_who_judges_itself_is_reported_when_someone_else_could():
    room = [_seat(id="p_codex", roles=["driver", "reviewer"]),
            _seat(id="p_claude", roles=["critic"])]

    problems = cp.check_role_conflicts(room)

    assert len(problems) == 1
    assert "p_codex" in problems[0]
    assert "driver" in problems[0] and "reviewer" in problems[0]
    assert "p_claude" in problems[0]  # the alternative is named, not just implied


def test_the_same_conflict_is_not_reported_when_the_room_has_no_alternative():
    """§5 says "cuando exista una alternativa", and §1.9.10 lets the room
    degrade to a single agent.  Refusing a lone model the right to review its
    own work would leave the user with nothing at all."""
    room = [_seat(id="p_codex", roles=["driver", "reviewer"])]

    assert cp.check_role_conflicts(room) == []

    both_implement = [_seat(id="p_codex", roles=["driver", "judge"]),
                      _seat(id="p_glm", roles=["integrator"])]
    assert cp.check_role_conflicts(both_implement) == []


def test_a_clean_room_reports_nothing():
    room = [_seat(id="p_codex", roles=["driver"]),
            _seat(id="p_claude", roles=["reviewer"]),
            _seat(id="p_glm", roles=["judge"])]

    assert cp.check_role_conflicts(room) == []


def test_check_role_conflicts_reads_resolved_participants_too(session):
    """The caller holds `ResolvedParticipant`s; making it unwrap first would be
    a rule nobody remembers on the one call site that matters."""
    seated = cp.resolve_participants(
        [cp.ParticipantSpec(display_name="Codex", model="m", roles=("driver", "judge")),
         cp.ParticipantSpec(display_name="Claude", model="m2", roles=("critic",))],
        session=session, owner="luis")

    problems = cp.check_role_conflicts(seated)

    assert len(problems) == 1
    assert "p_codex" in problems[0]


# --- rule 5: nobody goes first twice (§22) ---------------------------------

ROOM = ("p_claude", "p_codex", "p_glm", "p_qwen", "p_gemini", "p_mistral")


def test_the_same_seed_always_gives_the_same_order():
    """A turn has to be replayable and a recovered session has to resume the
    order it had, so the permutation is a pure function of (seed, ids)."""
    first = cp.rotation_order(ROOM, seed="turn_7")
    second = cp.rotation_order(list(reversed(ROOM)), seed="turn_7")

    assert first == cp.rotation_order(ROOM, seed="turn_7")
    assert sorted(first) == sorted(ROOM)
    # The order is a property of the seed and the ids, not of how the room was
    # handed over: reversing the input must not reverse the round.
    assert second == first


def test_different_seeds_give_different_orders_and_none_of_them_is_insertion():
    """§22: "un modelo domina por orden o reputación".  Insertion order is
    exactly that risk, so the test asserts the permutation actually moves.

    Not a probabilistic claim: `rotation_order` hashes, so these six answers
    are fixed for all time and this either passes forever or fails forever.
    """
    orders = [tuple(cp.rotation_order(ROOM, seed=f"turn_{n}")) for n in range(6)]

    assert len(set(orders)) > 1
    assert any(order != ROOM for order in orders)
    assert all(sorted(order) == sorted(ROOM) for order in orders)


def test_rotation_order_collapses_a_duplicated_seat():
    assert cp.rotation_order(["p_a", "p_a", "p_b"], seed="x") in (["p_a", "p_b"],
                                                                 ["p_b", "p_a"])
    assert cp.rotation_order([], seed="x") == []


# --- rule 7: suggesting roles is a table, not a model call -----------------

SPECS = [cp.ParticipantSpec(display_name="Claude", model="claude-4"),
         cp.ParticipantSpec(display_name="Codex", model="qwen3-coder"),
         cp.ParticipantSpec(display_name="Glm", model="glm-4.6")]


def test_suggest_roles_is_deterministic_and_says_why():
    first = cp.suggest_roles(SPECS, policy="debate")
    again = cp.suggest_roles(SPECS, policy="debate")
    why = cp.explain_roles(SPECS, policy="debate")

    assert first == again
    assert first == {"p_claude": ("architect",), "p_codex": ("critic",), "p_glm": ("judge",)}
    assert set(why) == set(first)
    assert all(line for line in why.values())
    assert "seat 1" in why["p_claude"]


def test_each_policy_gets_the_shape_section_4_describes():
    collaborate = cp.suggest_roles(SPECS, policy="collaborate")
    pair = cp.suggest_roles(SPECS[:2], policy="pair")
    tournament = cp.suggest_roles(SPECS, policy="tournament")

    assert collaborate["p_codex"] == ("driver",)
    assert collaborate["p_glm"] == ("reviewer",)
    assert pair == {"p_claude": ("driver",), "p_codex": ("reviewer",)}
    # §4.6: a tournament needs somebody holding the rubric.
    assert tournament["p_glm"] == ("judge",)
    assert cp.check_role_conflicts(
        [_seat(id=seat, roles=list(roles)) for seat, roles in collaborate.items()]) == []


def test_a_declared_role_is_never_overwritten_by_a_suggestion():
    specs = [cp.ParticipantSpec(display_name="Claude", roles=("judge",)),
             cp.ParticipantSpec(display_name="Codex")]

    roles = cp.suggest_roles(specs, policy="collaborate")
    why = cp.explain_roles(specs, policy="collaborate")

    assert roles["p_claude"] == ("judge",)
    assert "declared" in why["p_claude"]


def test_a_task_hint_adds_one_role_nobody_held():
    plain = cp.suggest_roles(SPECS, policy="debate")
    hinted = cp.suggest_roles(SPECS, policy="debate", task_hint="add a pytest regression first")

    assert "tester" not in [r for roles in plain.values() for r in roles]
    assert "tester" in [r for roles in hinted.values() for r in roles]
    # Deterministic, and it never lands on a seat that implements.
    assert hinted == cp.suggest_roles(SPECS, policy="debate",
                                      task_hint="add a pytest regression first")


def test_an_unknown_role_in_a_spec_is_dropped_not_invented():
    specs = [cp.ParticipantSpec(display_name="Claude", roles=("wizard",))]

    assert cp.suggest_roles(specs, policy="chat") == {"p_claude": ("coordinator",)}


def test_ids_are_stable_and_never_collide():
    twice = [cp.ParticipantSpec(display_name="Claude"), cp.ParticipantSpec(display_name="Claude")]

    assert cp.participant_ids(twice) == ["p_claude", "p_claude_2"]
    assert cp.participant_id(cp.ParticipantSpec(display_name="the user")) == "p_the_user"
