"""Tests for src/council/policies.py -- the rules of section 9.1 and 4.

Every hard rule in the plan gets a test here, and each test is named after the
sentence it is defending:

    - the deterministic router runs FIRST, and answers `None` rather than
      guessing when the rules do not decide (9.1);
    - `@Claude` runs Claude and nobody else (20);
    - "responded todos de forma independiente" runs blind (20);
    - the intervention cap is 2 in `chat` and 1 per participant and phase in
      `debate` (9.3);
    - a sequential round speaks in `rotation_order(seed=turn_id)`, not in the
      order somebody added the seats (20, 22);
    - `debate` has the five phases of 4.3, in order, and nobody appears twice
      in one phase;
    - `pair` has exactly one writer (4.5);
    - and no policy grants anything at all (11.1, 25).

Nothing here calls a model, opens a database or touches the filesystem: a
policy is a pure function of a message, a room and a seed, and a test that
needed anything else would be testing something other than the policy.
"""
from __future__ import annotations

import ast
import inspect
from typing import Sequence

import pytest

from src.council.contracts import POLICIES, CouncilError, CouncilParticipant, CouncilTurn
from src.council.participants import rotation_order
from src.council import policies as policies_module
from src.council.policies import (
    BLIND_PARALLEL,
    BLINDNESS_VALUES,
    DEBATE_PHASES,
    PARALLEL,
    PEER_OUTPUTS_HIDDEN,
    SEQUENTIAL,
    Selection,
    is_social,
    known_policies,
    mentions,
    policy_for,
    route,
)


def _p(pid: str, name: str, roles: Sequence[str] = (), profile: str = "read_only"):
    return CouncilParticipant(id=pid, display_name=name, roles=tuple(roles),
                              tool_profile=profile, model=f"provider/{pid}")


def _room():
    """Four seats, one of each function the router cares about."""
    return [
        _p("p_claude", "Claude", ("architect", "reviewer")),
        _p("p_codex", "Codex", ("driver",), profile="scoped_write"),
        _p("p_qwen", "Qwen", ("critic",)),
        _p("p_mistral", "Mistral", ("coordinator",)),
    ]


def _turn(content: str, turn_id: str = "turn_0001", **kw) -> CouncilTurn:
    return CouncilTurn(id=turn_id, session_id="council_test", content=content, **kw)


# --- rule 1: the deterministic router goes first (9.1) ---------------------

def test_a_mention_selects_only_the_mentioned_participant():
    chosen = route("@Claude, what do you make of this?", _room())
    assert chosen is not None
    assert chosen.participant_ids == ("p_claude",)
    assert chosen.mode == SEQUENTIAL


def test_a_mention_is_matched_by_id_display_name_and_case():
    room = _room()
    for spelling in ("@claude", "@Claude", "@p_claude", "@CLAUDE"):
        assert mentions(spelling, room) == ("p_claude",), spelling


def test_asking_the_room_selects_everybody_in_parallel_and_blind():
    for message in ("Responded todos de forma independiente",
                    "I want each of you to answer independently"):
        chosen = route(message, _room())
        assert chosen is not None, message
        assert len(chosen.participant_ids) == 4
        assert chosen.mode == BLIND_PARALLEL


def test_a_scope_that_says_todos_is_not_an_audience():
    """"revisad todos los tests" is a scope, not the room.

    Without the negative lookahead this sentence -- the most common one in a
    code review -- would route to every participant in parallel, which is the
    single most expensive way to misread a message."""
    chosen = route("revisad todos los tests que fallan", _room())
    assert chosen is not None
    assert chosen.mode == SEQUENTIAL
    assert len(chosen.participant_ids) < 4


def test_asking_for_a_rebuttal_selects_a_proposer_and_a_critic():
    for message in ("Rebatid esta propuesta", "compare these two designs",
                    "revisa el diseño"):
        chosen = route(message, _room())
        assert chosen is not None, message
        assert "p_qwen" in chosen.participant_ids, message  # the critic
        assert len(chosen.participant_ids) == 2, message


def test_asking_for_work_selects_the_coordinator_the_driver_and_the_reviewer():
    for message in ("implementa el callback de OAuth", "fix the login redirect"):
        chosen = route(message, _room())
        assert chosen is not None, message
        assert "p_codex" in chosen.participant_ids, message      # the driver
        assert "p_mistral" in chosen.participant_ids, message    # the coordinator
        assert "p_claude" in chosen.participant_ids, message     # the reviewer
        assert chosen.mode == SEQUENTIAL


def test_a_reply_to_a_concrete_message_selects_its_author_and_a_reviewer():
    room = _room()
    history = [{"id": "msg_1", "author_id": "p_codex", "content": "here is my design"}]
    answer = {"id": "msg_2", "content": "no me convence del todo", "reply_to": "msg_1"}
    chosen = route(answer, room, history=history)
    assert chosen is not None
    assert chosen.participant_ids[0] == "p_codex"
    assert len(chosen.participant_ids) == 2


def test_a_reply_to_a_message_nobody_passed_does_not_invent_an_author():
    """The rule needs the transcript; guessing the author from the text is the
    one thing 3.2 forbids outright."""
    answer = {"id": "msg_2", "content": "no me convence del todo", "reply_to": "msg_1"}
    assert route(answer, _room(), history=()) is None


def test_a_greeting_is_answered_by_one_participant_and_not_by_the_room():
    chosen = route("hola, buenos dias", _room())
    assert chosen is not None
    assert len(chosen.participant_ids) == 1
    assert is_social("hola") and is_social("thanks!")


def test_a_greeting_with_work_attached_is_not_social():
    assert not is_social("hola, implementad el callback de OAuth")


def test_the_router_answers_none_when_the_rules_do_not_decide():
    """`None` is the signal, not a failure: it is the only case worth paying a
    coordinator model for (9.1)."""
    assert route("el rendimiento del indice me preocupa desde ayer", _room()) is None


def test_the_router_answers_none_for_an_empty_room():
    assert route("@claude hola", []) is None


# --- rule 2: @Claude runs Claude (20) --------------------------------------

def test_a_mention_beats_every_other_rule():
    """Even when the message also asks for work, or says "todos"."""
    room = _room()
    chosen = route("@Claude implementa esto y que opinen todos", room)
    assert chosen is not None
    assert chosen.participant_ids == ("p_claude",)


def test_a_mention_survives_the_policy_that_would_have_chosen_otherwise():
    room = _room()
    for name in known_policies():
        selection = policy_for(name).select(
            session=None, participants=room, message=_turn("@Claude revisa esto"),
            ledger=None, history=())
        assert selection.participant_ids == ("p_claude",), name


# --- rule 3: an independent round shares nothing (20) ----------------------

def test_an_independent_round_hides_the_peers_answers():
    chosen = route("responded todos de forma independiente", _room())
    assert chosen is not None
    assert chosen.mode == "blind_parallel"
    assert chosen.blindness == "peer_outputs_hidden"


def test_consult_is_blind_even_when_the_message_says_nothing_about_it():
    selection = policy_for("consult").select(
        session=None, participants=_room(), message=_turn("¿donde valido el state de OAuth?"),
        ledger=None, history=())
    assert selection.mode == BLIND_PARALLEL
    assert selection.blindness == PEER_OUTPUTS_HIDDEN
    assert len(selection.participant_ids) == 4


def test_the_blindness_vocabulary_matches_the_context_compilers():
    from src.council.context import BLINDNESS

    assert tuple(BLINDNESS) == tuple(BLINDNESS_VALUES)


# --- rule 4: the intervention cap (9.3) ------------------------------------

def test_chat_answers_at_most_twice_per_turn():
    policy = policy_for("chat")
    assert policy.intervention_cap() == 2
    selection = policy.select(session=None, participants=_room(),
                              message=_turn("implementa el callback de OAuth"),
                              ledger=None, history=())
    assert len(selection.participant_ids) == 2
    assert "capped at 2" in selection.reason


def test_chat_is_not_capped_when_the_user_asked_the_whole_room():
    policy = policy_for("chat")
    for message in ("@todos, que opinais", "responded todos de forma independiente"):
        selection = policy.select(session=None, participants=_room(), message=_turn(message),
                                  ledger=None, history=())
        assert len(selection.participant_ids) == 4, message
        assert "capped" not in selection.reason, message


def test_debate_allows_one_intervention_per_participant_and_phase():
    policy = policy_for("debate")
    assert policy.intervention_cap() == 1
    plan = policy.plan(session=None, participants=_room(), message=None, ledger=None)
    for phase in plan.phases:
        seats = plan.per_phase[phase].participant_ids
        assert len(seats) == len(set(seats)), phase


# --- rule 5: the round speaks in rotation order (20, 22) -------------------

def test_a_sequential_round_speaks_in_rotation_order_and_not_insertion_order():
    room = _room()
    policy = policy_for("collaborate")
    differed = ""
    for index in range(200):
        turn = _turn("implementa el callback de OAuth", turn_id=f"turn_{index:04d}")
        selection = policy.select(session=None, participants=room, message=turn,
                                  ledger=None, history=())
        assert selection.mode == SEQUENTIAL
        chosen = set(selection.participant_ids)
        expected = tuple(seat for seat in rotation_order(room, seed=turn.id) if seat in chosen)
        assert selection.participant_ids == expected, turn.id
        insertion = tuple(seat.id for seat in room if seat.id in chosen)
        if selection.participant_ids != insertion:
            differed = turn.id
    assert differed, ("no seed in 200 produced an order different from insertion order; the "
                      "rotation is not keyed on the turn id")


def test_the_same_seed_always_produces_the_same_order():
    """A recovered or replayed turn must speak in the order it spoke in."""
    room = _room()
    policy = policy_for("collaborate")
    turn = _turn("implementa el callback", turn_id="turn_stable")
    first = policy.select(session=None, participants=room, message=turn, ledger=None, history=())
    second = policy.select(session=None, participants=list(reversed(room)), message=turn,
                           ledger=None, history=())
    assert first.participant_ids == second.participant_ids


# --- rule 6: debate has five phases, in order (4.3) ------------------------

def test_debate_has_the_five_phases_of_the_plan_in_order():
    plan = policy_for("debate").plan(session=None, participants=_room(), message=None,
                                     ledger=None)
    assert plan.phases == ("proposal", "critique", "rebuttal", "synthesis", "verdict")
    assert plan.phases == DEBATE_PHASES


def test_no_participant_appears_twice_in_one_debate_phase():
    room = _room() + [_p("p_extra", "Claude", ("architect",))]  # a duplicated display name
    plan = policy_for("debate").plan(session=None, participants=room, message=None, ledger=None)
    for phase, selection in plan.per_phase.items():
        seats = selection.participant_ids
        assert len(seats) == len(set(seats)), f"{phase} lists a participant twice"


def test_the_debate_critics_are_not_the_proposers():
    plan = policy_for("debate").plan(session=None, participants=_room(), message=None,
                                     ledger=None)
    proposals = set(plan.per_phase["proposal"].participant_ids)
    critiques = set(plan.per_phase["critique"].participant_ids)
    assert proposals and critiques
    assert not (proposals & critiques)


def test_the_first_debate_round_is_blind():
    plan = policy_for("debate").plan(session=None, participants=_room(), message=None,
                                     ledger=None)
    assert plan.per_phase["proposal"].mode == BLIND_PARALLEL
    assert plan.per_phase["proposal"].blindness == PEER_OUTPUTS_HIDDEN
    assert plan.per_phase["critique"].mode == PARALLEL


def test_a_debate_round_budget_comes_from_the_session_and_not_from_the_policy():
    class _Budgets:
        max_rounds = 0

    class _Session:
        budgets = _Budgets()
        phase = ""
        id = "council_1"

    plan = policy_for("debate").plan(session=_Session(), participants=_room(), message=None,
                                     ledger=None)
    assert plan.max_rounds == 0, "a room told to run no further rounds was given four"


# --- rule 7: a pair has one writer (4.5) -----------------------------------

def test_pair_never_selects_two_writers():
    room = _room() + [_p("p_second", "Second", ("driver",), profile="scoped_write")]
    policy = policy_for("pair")
    for message in ("implementa el login", "revisa el diff", "hola", "@todos ayudadme",
                    "algo completamente ambiguo"):
        selection = policy.select(session=None, participants=room, message=_turn(message),
                                  ledger=None, history=())
        writers = [seat for seat in selection.participant_ids
                   if seat in ("p_codex", "p_second")]
        assert len(writers) <= 1, f"{message} selected {writers}"


def test_pair_selects_a_driver_and_a_navigator():
    selection = policy_for("pair").select(
        session=None, participants=_room(), message=_turn("implementa el login"),
        ledger=None, history=())
    assert len(selection.participant_ids) == 2
    assert "p_codex" in selection.participant_ids
    assert selection.mode == SEQUENTIAL


def test_the_pair_implement_phase_has_exactly_one_participant():
    room = _room() + [_p("p_second", "Second", ("driver",), profile="scoped_write")]
    plan = policy_for("pair").plan(session=None, participants=room, message=None, ledger=None)
    assert len(plan.per_phase["implement"].participant_ids) == 1


# --- rule 8: no policy grants anything (11.1, 25) --------------------------

FORBIDDEN_NAMES = frozenset({
    "can_write", "can_participant_write", "WRITING_PROFILES", "TOOL_PROFILES",
    "ROLE_TOOL_PROFILES", "role_default_profile", "effective_profile",
    "policy_ceiling", "POLICY_TOOL_CEILINGS", "UNKNOWN_POLICY_CEILING",
    "FileLockRegistry", "ApprovalStore",
})


def test_a_selection_has_no_permission_field():
    names = tuple(Selection.__dataclass_fields__)
    assert names == ("participant_ids", "mode", "reason", "phase", "blindness")
    for name in names:
        for word in ("profile", "permission", "tool", "grant", "write", "approve"):
            assert word not in name, f"{name} looks like a permission"


def test_policies_import_nothing_that_computes_a_permission():
    """The rule is enforced by what the module can REACH, not by a comment.

    A policy that imported `can_write` would be one refactor away from calling
    it, and a room would then have two answers to "who may write" (11.1)."""
    tree = ast.parse(inspect.getsource(policies_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    assert not (imported & FORBIDDEN_NAMES), sorted(imported & FORBIDDEN_NAMES)
    assert not (set(dir(policies_module)) & FORBIDDEN_NAMES)


def test_no_selection_produced_by_any_policy_carries_a_permission():
    room = _room()
    for name in known_policies():
        policy = policy_for(name)
        selection = policy.select(session=None, participants=room,
                                  message=_turn("implementa el callback"), ledger=None,
                                  history=())
        assert set(selection.to_dict()) == {"participant_ids", "mode", "reason", "phase",
                                            "blindness"}, name


# --- the registry ----------------------------------------------------------

def test_every_declared_policy_is_implemented():
    assert tuple(known_policies()) == tuple(POLICIES)


def test_an_unknown_policy_is_refused_and_never_falls_back_to_chat():
    with pytest.raises(CouncilError) as caught:
        policy_for("committee")
    assert "committee" in str(caught.value)


def test_every_selection_uses_a_declared_mode_and_blindness():
    room = _room()
    for name in known_policies():
        policy = policy_for(name)
        for message in ("hola", "implementa el login", "rebatid la propuesta",
                        "responded todos", "@Claude que opinas", "algo ambiguo"):
            selection = policy.select(session=None, participants=room, message=_turn(message),
                                      ledger=None, history=())
            assert selection.mode in ("sequential", "parallel", "blind_parallel")
            assert selection.blindness in BLINDNESS_VALUES
            assert selection.reason, f"{name}/{message} selected without saying why"


def test_a_policy_never_selects_a_participant_who_is_not_in_the_room():
    room = _room()
    seats = {seat.id for seat in room}
    for name in known_policies():
        policy = policy_for(name)
        selection = policy.select(session=None, participants=room,
                                  message=_turn("@nobody @claude implementa"), ledger=None,
                                  history=())
        assert set(selection.participant_ids) <= seats, name


def test_an_empty_room_selects_nobody_instead_of_raising():
    for name in known_policies():
        selection = policy_for(name).select(session=None, participants=[],
                                            message=_turn("implementa"), ledger=None, history=())
        assert selection.participant_ids == ()
