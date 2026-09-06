"""
The budget arithmetic, pinned.

Two of these tests exist because a comment claimed something the code did not
do.  `budgets.IMAGE_BLOCK_TOKENS` says it is kept in step with
`src.model_context.IMAGE_BLOCK_TOKENS`, and `AppParityEstimator` says it
reproduces `estimate_tokens` exactly.  Both were prose until this file; a
constant that is "kept in sync" by a sentence in a docstring is a constant that
drifts on the first afternoon nobody re-reads the docstring.

The rest guard the invariant the whole subsystem rests on: reserves come off
the top, and a window nobody could prove is not a window we may spend.
"""

from __future__ import annotations

import random
import string

import pytest

from src import model_context
from src.context_engine import budgets
from src.context_engine.contracts import SECTION_KINDS, ContextBudget, ContractError


# ── the two claims that used to live only in comments ──────────────────────

def test_the_image_block_price_is_the_one_the_rest_of_the_app_charges():
    """A screenshot must not cost 1200 tokens to the trim gate and 0 here."""
    assert budgets.IMAGE_BLOCK_TOKENS == model_context.IMAGE_BLOCK_TOKENS


def _random_messages(rng: random.Random, count: int):
    alphabet = string.ascii_letters + string.digits + " .,\n"
    out = []
    for _ in range(count):
        roll = rng.random()
        if roll < 0.35:
            body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
            out.append({"role": "user", "content": body})
        elif roll < 0.6:
            blocks = [{"type": "text", "text": "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 200)))}]
            if rng.random() < 0.5:
                blocks.append({"type": "image_url", "image_url": {"url": "data:x"}})
            out.append({"role": "user", "content": blocks})
        elif roll < 0.85:
            out.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": "".join(rng.choice(alphabet)
                                             for _ in range(rng.randint(0, 300))),
                    },
                }],
            })
        else:
            out.append({"role": "system", "content": ""})
    return out


def test_the_app_parity_estimator_matches_estimate_tokens_on_generated_input():
    """Exactly, not approximately.

    A shadow report whose difference is an artefact of two rulers sends
    somebody chasing a gap that was the measurement.
    """
    rng = random.Random(20260906)
    parity = budgets.app_parity_estimator()
    for _ in range(200):
        messages = _random_messages(rng, rng.randint(0, 8))
        assert parity.count_messages(messages) == model_context.estimate_tokens(messages)


def test_the_conservative_estimator_never_counts_fewer_tokens_than_the_app_one():
    """The default lane decides what fits, so it has to err towards 'fits less'."""
    rng = random.Random(7)
    conservative = budgets.ConservativeEstimator()
    parity = budgets.app_parity_estimator()
    for _ in range(100):
        text = "".join(rng.choice(string.printable[:60]) for _ in range(rng.randint(1, 500)))
        assert conservative.count(text) >= parity.count(text)


# ── reserves come off the top ──────────────────────────────────────────────

def test_reserves_are_taken_before_context_not_after():
    budget = budgets.resolve_budget(
        model="m", context_length=32768, window_known=True,
        max_output_tokens=4096, tool_schema_tokens=2000)
    assert budget.input_budget == 32768 - 4096 - 2000
    assert budget.input_budget + budget.reserved_output + budget.reserved_tools == 32768


def test_a_window_nobody_could_prove_is_not_a_window_we_spend():
    """`budget_context_for_model` answers 0 when the window is a guess.  The
    answer is a small floor, not the provider table's optimistic 128k."""
    budget = budgets.resolve_budget(model="m", context_length=0, window_known=False,
                                    max_output_tokens=4096)
    assert budget.window_known is False
    assert budget.max_tokens == budgets.DEFAULT_UNKNOWN_WINDOW
    assert budget.input_budget < budgets.DEFAULT_UNKNOWN_WINDOW


def test_a_claimed_window_without_proof_is_still_clamped_to_the_floor():
    budget = budgets.resolve_budget(model="m", context_length=128000,
                                    window_known=False, max_output_tokens=1024)
    assert budget.max_tokens == budgets.DEFAULT_UNKNOWN_WINDOW


def test_reserves_that_would_eat_the_window_are_clamped_not_negative():
    """A tool list bigger than the window used to produce a negative budget,
    which then read as 'unlimited' three layers down."""
    budget = budgets.resolve_budget(model="m", context_length=4096, window_known=True,
                                    max_output_tokens=8192, tool_schema_tokens=8192)
    assert budget.input_budget >= 0
    assert budget.reserved_output >= budgets.MIN_OUTPUT_RESERVE
    assert (budget.input_budget + budget.reserved_output
            + budget.reserved_tools) <= budget.max_tokens


def test_the_configured_budget_can_only_narrow_never_widen():
    wide = budgets.resolve_budget(model="m", context_length=8192, window_known=True,
                                  max_output_tokens=1024,
                                  configured_input_budget=999_999)
    assert wide.input_budget <= 8192 - 1024


def test_the_contract_refuses_a_budget_that_does_not_add_up():
    with pytest.raises(ContractError):
        ContextBudget(max_tokens=1000, reserved_output=600,
                      reserved_tools=600, input_budget=600)


# ── the split ──────────────────────────────────────────────────────────────

def test_allocation_spends_the_whole_input_budget_and_no_more():
    budget = budgets.resolve_budget(model="m", context_length=16384, window_known=True,
                                    max_output_tokens=2048)
    profile = budgets.profile_for(intent="code_change")
    present = ["system_constraints", "recent_messages", "retrieved_documents"]
    shares = budgets.allocate(budget, profile, present=present)
    assert sum(shares.values()) == budget.input_budget
    assert all(shares[k] == 0 for k in SECTION_KINDS if k not in present)


def test_an_absent_section_does_not_hold_a_share_hostage():
    """15% reserved for recipes on a Python bugfix is 15% wasted."""
    budget = budgets.resolve_budget(model="m", context_length=16384, window_known=True,
                                    max_output_tokens=2048)
    profile = budgets.PROFILES["media_v1"]
    with_recipes = budgets.allocate(budget, profile,
                                    present=["recent_messages", "multimodal_recipes"])
    without = budgets.allocate(budget, profile, present=["recent_messages"])
    assert without["recent_messages"] > with_recipes["recent_messages"]


def test_a_casual_chat_profile_gives_the_code_map_nothing():
    profile = budgets.profile_for(intent="chat")
    assert profile.profile_id == "conversation_v1"
    assert profile.share("code_map") == 0.0


def test_an_unknown_intent_degrades_to_the_agentic_default_not_to_no_context():
    assert budgets.profile_for(intent="something_new_2027").profile_id == "balanced_v1"


def test_an_explicit_profile_beats_the_intent():
    profile = budgets.profile_for(profile_id="research_v1", intent="chat")
    assert profile.profile_id == "research_v1"


def test_mandatory_sections_outrank_every_optional_one_when_trimming():
    profile = budgets.PROFILES["balanced_v1"]
    worst_mandatory = min(profile.priority(k) for k in
                          ("system_constraints", "role_and_permissions", "active_goal",
                           "current_state", "decisions"))
    best_optional = max(profile.priority(k) for k in
                        ("retrieved_memory", "retrieved_documents", "past_experiences",
                         "peer_findings", "multimodal_recipes", "code_map"))
    assert worst_mandatory > best_optional


def test_section_order_is_fixed_so_two_packets_differ_in_content_not_layout():
    scrambled = list(reversed(SECTION_KINDS))
    assert budgets.section_order(scrambled) == list(SECTION_KINDS)


def test_the_tool_list_is_priced_as_json_not_as_prose():
    schemas = [{"type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    assert budgets.tool_schema_tokens(schemas, model="m") > 0
    assert budgets.tool_schema_tokens([], model="m") == 0


# ── the estimator lane ─────────────────────────────────────────────────────

def test_an_unmapped_model_gets_the_conservative_lane_and_says_so():
    budgets.reset_estimator_cache()
    estimator = budgets.estimator_for("some-model-nobody-mapped")
    assert estimator.name == "heuristic"


def test_a_broken_tokenizer_map_does_not_take_the_turn_down(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_TOKENIZER_MAP", "{not json at all")
    budgets.reset_estimator_cache()
    assert budgets.estimator_for("anything").name == "heuristic"
    budgets.reset_estimator_cache()
