"""
Completion modes, pinned where they are easy to get wrong.

Four claims are load-bearing enough that prose would not have kept them true:

1. The precedence of §6 is walked, not hard-coded. The test drives all six
   sources and checks that the winner is named, because the value of this
   layer is not "greedy" but "greedy, and here is which level asked for it".

2. Reading a mode out of a sentence is conservative. Spanish and English cues
   both resolve, and anything with two readings resolves to `""` — a wrong
   `maximalist` spends an afternoon of GPU nobody authorised.

3. The four mid-flight transitions do what §6 says, in both directions, and
   never lose the budget already spent.

4. A mode cannot express a permission. That one is checked against the
   dataclass field names rather than against behaviour, because behaviour can
   be correct today and a new field can make it wrong tomorrow.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from src.agent_profiles import completion, contracts


# ── the catalogue of modes ─────────────────────────────────────────────────

def test_the_four_modes_are_exactly_the_ones_the_contract_declares():
    assert set(completion.MODE_ORDER) == set(contracts.COMPLETION_MODES)
    assert set(completion.POLICIES) == set(contracts.COMPLETION_MODES)
    versions = [p.policy_version for p in completion.POLICIES.values()]
    assert len(set(versions)) == len(versions), "policy versions must be distinct"
    for mode, policy in completion.POLICIES.items():
        assert policy.mode == mode
        assert policy.policy_version.endswith("_v1")


def test_an_unknown_mode_is_rejected_by_name():
    with pytest.raises(contracts.ProfileError) as raised:
        completion.policy_for("thorough")
    message = str(raised.value)
    for known in completion.MODE_ORDER:
        assert known in message


def test_narrows_orders_the_modes_and_is_strict():
    assert completion.narrows("greedy", "literal") is True
    assert completion.narrows("maximalist", "professional") is True
    assert completion.narrows("literal", "greedy") is False
    assert completion.narrows("greedy", "greedy") is False


# ── precedence (§6) ────────────────────────────────────────────────────────

#: One distinct mode per source, so the answer alone identifies the winner.
_PER_SOURCE = {
    "activity": "literal",
    "task": "professional",
    "run": "greedy",
    "agent_default": "maximalist",
    "project_default": "literal",
    "global_default": "professional",
}


def test_the_precedence_walks_all_six_sources_and_says_which_one_won():
    """Every level, in order, wins over everything below it.

    The loop starts with all six offered and removes the top one each time, so
    the same run exercises "activity wins" and "nothing above global_default
    said anything" and every step between.

    The winner is checked against `contracts.OVERRIDE_SOURCES` rather than
    against this module's own spelling. `completion` uses the short names
    (`task`) and the record uses the long ones (`task_override`); the contract
    maps between them, and this is what pins the two ladders to the same order
    instead of merely to the same set of words.
    """
    order = completion.MODE_PRECEDENCE
    assert order == ("activity", "task", "run",
                     "agent_default", "project_default", "global_default")
    assert len(order) == len(contracts.OVERRIDE_SOURCES)

    winners = []
    for index, source in enumerate(order):
        offered = {name: _PER_SOURCE[name] for name in order[index:]}
        choice = completion.resolve_mode(**offered)

        assert choice.source == contracts.OVERRIDE_SOURCES[index]
        assert choice.mode == _PER_SOURCE[source]
        assert choice.policy_version == completion.POLICIES[choice.mode].policy_version
        assert source in choice.reason
        for skipped in order[:index]:
            assert skipped in choice.reason, "a silent level must still be named"
        for beaten in order[index + 1:]:
            assert beaten in choice.reason, "an outranked level must still be named"
        winners.append(choice.source)

    assert winners == list(contracts.OVERRIDE_SOURCES)


def test_the_global_default_is_the_floor_when_nobody_else_speaks():
    choice = completion.resolve_mode()
    assert choice.mode == "greedy"
    assert choice.source == "global_default"
    assert "no value at" in choice.reason


def test_a_bad_mode_is_rejected_with_the_level_that_offered_it():
    with pytest.raises(contracts.ProfileError) as raised:
        completion.resolve_mode(task="thorough")
    assert "task" in str(raised.value)


def test_no_level_offering_anything_is_a_rejection_not_a_guess():
    with pytest.raises(contracts.ProfileError):
        completion.resolve_mode(global_default="")


# ── reading a mode out of an instruction ───────────────────────────────────

@pytest.mark.parametrize("sentence, expected", [
    ("Solo cambia el parser de fechas, nada mas.", "literal"),
    ("Sólo cambia esa línea.", "literal"),
    ("Cambia únicamente el import roto", "literal"),
    ("Hazlo completo: tests y manejo de errores.", "professional"),
    ("Ya que estás, arregla lo de al lado", "greedy"),
    ("Dame muchas opciones de portada", "maximalist"),
    ("dame opciones", "maximalist"),
])
def test_from_instruction_reads_spanish(sentence, expected):
    assert completion.from_instruction(sentence) == expected


@pytest.mark.parametrize("sentence, expected", [
    ("Only change the date parser, nothing else.", "literal"),
    ("Just fix the failing import", "literal"),
    ("Make it complete, with tests.", "professional"),
    ("While you're at it, tidy the neighbour", "greedy"),
    ("Give me options for the cover", "maximalist"),
    ("Explore every alternative you can find", "maximalist"),
])
def test_from_instruction_reads_english(sentence, expected):
    assert completion.from_instruction(sentence) == expected


@pytest.mark.parametrize("sentence", [
    "",
    "   ",
    "Arregla el bug del login",
    "Fix the login bug",
    "Necesito que esto quede mejor",
    "Solo cambia esto, pero dame opciones",       # two readings at once
    "Only change this, but give me options",      # the same, in English
    "No solo cambies eso",                        # the cue, negated
    "Do not touch anything else is not what I meant",
])
def test_from_instruction_returns_nothing_when_it_is_not_certain(sentence):
    """`""` means "ask the precedence"; a guess here would look like a decision."""
    assert completion.from_instruction(sentence) == ""


def test_from_instruction_is_deterministic():
    sentence = "Sólo cambia el parser, nada más"
    assert len({completion.from_instruction(sentence) for _ in range(50)}) == 1


# ── changing mode mid-flight (§6) ──────────────────────────────────────────

def test_greedy_to_literal_starts_no_new_bonus_but_finishes_what_it_began():
    moved = completion.transition("greedy", "literal", spent_budget=1234.5)
    assert moved["from"] == "greedy" and moved["to"] == "literal"
    assert moved["start_new_bonus"] is False
    assert moved["finish_started_effects"] is True
    assert moved["regenerate_frontier"] is False
    assert moved["keep_spent"] == 1234.5
    assert moved["audit"]["direction"] == "narrower"


def test_literal_to_greedy_builds_a_frontier_without_redoing_the_core():
    moved = completion.transition("literal", "greedy", spent_budget=10)
    assert moved["regenerate_frontier"] is True
    assert moved["start_new_bonus"] is True
    assert moved["audit"]["repeat_core"] is False
    assert moved["audit"]["direction"] == "wider"
    assert moved["keep_spent"] == 10


def test_greedy_to_maximalist_widens_the_frontier_and_keeps_the_spend():
    moved = completion.transition("greedy", "maximalist", spent_budget=2000)
    assert moved["regenerate_frontier"] is True
    assert moved["start_new_bonus"] is True
    assert moved["keep_spent"] == 2000
    assert moved["audit"]["to_policy_version"] == "maximalist_v1"


def test_maximalist_to_greedy_narrows_without_tearing_down_the_frontier():
    """Narrowing stops the *widening*, not the work already under way.

    `greedy` still has a bonus share of its own, so narrowing into it does not
    forbid bonus work the way narrowing into `literal` does; what stops is the
    frontier regeneration, and the 999 already spent stays spent.
    """
    moved = completion.transition("maximalist", "greedy", spent_budget=999)
    assert moved["audit"]["direction"] == "narrower"
    assert moved["regenerate_frontier"] is False
    assert moved["start_new_bonus"] is True
    assert moved["finish_started_effects"] is True
    assert moved["keep_spent"] == 999


def test_a_transition_never_touches_tools_or_permissions():
    for current in completion.MODE_ORDER:
        for target in completion.MODE_ORDER:
            audit = completion.transition(current, target, spent_budget=0)["audit"]
            assert audit["tools_changed"] is False
            assert audit["permissions_changed"] is False


@pytest.mark.parametrize("kwargs", [
    {"current": "greedy", "target": "thorough", "spent_budget": 1},
    {"current": "thorough", "target": "greedy", "spent_budget": 1},
    {"current": "greedy", "target": "literal", "spent_budget": -1},
    {"current": "greedy", "target": "literal", "spent_budget": "a lot"},
])
def test_a_transition_rejects_nonsense_rather_than_guessing(kwargs):
    with pytest.raises(contracts.ProfileError):
        completion.transition(kwargs["current"], kwargs["target"],
                              spent_budget=kwargs["spent_budget"])


# ── §3.3: a mode grants nothing ────────────────────────────────────────────

#: Words that would mean this type had started describing authority.
_AUTHORITY_WORDS = (
    "tool", "permission", "deny", "allow", "grant", "path", "root",
    "effect", "scope", "secret", "network", "write", "read", "trust",
)


def test_a_completion_policy_has_no_field_in_which_a_permission_could_live():
    """§3.3, enforced structurally.

    `maximalist` on a read-only reviewer stays read-only because there is
    nowhere in this dataclass to say otherwise. Checking the field names rather
    than the behaviour is the point: behaviour can be right today and a new
    field can make it wrong next month without any test noticing.
    """
    names = [f.name for f in dataclass_fields(completion.CompletionPolicy)]
    assert names, "the dataclass must have fields for this test to mean anything"
    for name in names:
        for word in _AUTHORITY_WORDS:
            assert word not in name, (
                "CompletionPolicy.{} looks like authority; a mode is depth "
                "only (plan §3.3)".format(name)
            )


def test_a_completion_policy_carries_data_and_never_a_callable():
    for policy in completion.POLICIES.values():
        for field in dataclass_fields(policy):
            value = getattr(policy, field.name)
            assert isinstance(value, (str, bool, int, float)), field.name
            assert not callable(value), field.name
