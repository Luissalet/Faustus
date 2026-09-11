"""tests/test_cmp07_workflow_iteration.py — CMP-07 (W2-E, CONTRATO_CMP_W2.md).

`src/contracts/workflow_iteration.py` is a PROPOSED contract: dataclasses
and validation only, never imported by the engine or any route. These tests
pin (1) that it stays that way — nothing here reaches into `workflows/`,
and `WorkflowDefinition`'s real `NODE_TYPES`/`_find_cycle` are untouched —
and (2) the shape itself: budgets, the early-exit condition, per-iteration
state, and the per-(effect, iteration) idempotency key.

`-p no:cacheprovider -W ignore` per COMUN.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from src.contracts.base import ContractError
from src.contracts.workflow import NODE_TYPES
from src.contracts.workflow_iteration import (
    LOOP_OPERATORS,
    IterationState,
    LoopBudget,
    LoopNodeConfig,
    LoopUntil,
    loop_effect_idempotency_key,
)


# ── this stays a proposal, not a wired feature ──────────────────────────────

def test_loop_is_not_a_real_node_type_yet():
    assert "loop" not in NODE_TYPES


def test_module_never_imports_the_engine_or_handlers():
    import src.contracts.workflow_iteration as m
    tree = ast.parse(inspect.getsource(m))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("workflows" in name for name in imported), imported


def test_loop_operators_stay_in_sync_with_the_real_condition_handler():
    from src.workflows.handlers import OPERATORS as REAL_OPERATORS
    assert LOOP_OPERATORS == REAL_OPERATORS


# ── LoopBudget ───────────────────────────────────────────────────────────────

def test_budget_requires_max_iterations():
    with pytest.raises(ContractError):
        LoopBudget.parse({})


def test_budget_max_iterations_is_never_unlimited_by_zero():
    with pytest.raises(ContractError):
        LoopBudget.parse({"max_iterations": 0})


def test_budget_round_trips():
    b = LoopBudget.parse({"max_iterations": 5, "max_tokens": 10_000, "on_exhausted": "fail"})
    assert b.to_dict() == {"max_iterations": 5, "max_tokens": 10_000, "max_seconds": 0,
                           "max_tool_calls": 0, "on_exhausted": "fail"}


def test_budget_defaults_on_exhausted_to_pause_not_fail():
    b = LoopBudget.parse({"max_iterations": 3})
    assert b.on_exhausted == "pause"


def test_budget_exhausted_by_checks_iterations_first():
    b = LoopBudget.parse({"max_iterations": 3, "max_tokens": 100})
    assert b.exhausted_by(iterations_used=3, tokens_used=0) == "max_iterations"
    assert b.exhausted_by(iterations_used=2, tokens_used=100) == "max_tokens"
    assert b.exhausted_by(iterations_used=1, tokens_used=1) is None


def test_budget_zero_dimension_is_unlimited():
    b = LoopBudget.parse({"max_iterations": 100})
    assert b.exhausted_by(iterations_used=1, tokens_used=10**9, seconds_used=10**9,
                          tool_calls_used=10**9) is None


# ── LoopUntil ────────────────────────────────────────────────────────────────

def test_until_requires_right_unless_exists_or_truthy():
    with pytest.raises(ContractError):
        LoopUntil.parse({"left": {"path": "loop.iteration"}, "op": "gte"})
    ok = LoopUntil.parse({"left": {"path": "loop.iteration"}, "op": "gte", "right": 3})
    assert ok.op == "gte" and ok.right == 3


def test_until_truthy_needs_no_right():
    u = LoopUntil.parse({"left": {"path": "loop.results.check.done"}})
    assert u.op == "truthy"


def test_until_rejects_an_unknown_operator():
    with pytest.raises(ContractError):
        LoopUntil.parse({"left": 1, "op": "regex", "right": ".*"})


# ── LoopNodeConfig ───────────────────────────────────────────────────────────

def _cfg(**over):
    base = {"body": ["draft", "review"], "budget": {"max_iterations": 4}}
    base.update(over)
    return base


def test_config_requires_a_non_empty_body():
    with pytest.raises(ContractError):
        LoopNodeConfig.parse(_cfg(body=[]))


def test_config_rejects_duplicate_body_ids():
    with pytest.raises(ContractError):
        LoopNodeConfig.parse(_cfg(body=["draft", "draft"]))


def test_config_requires_a_budget():
    with pytest.raises(ContractError):
        LoopNodeConfig.parse({"body": ["draft"]})


def test_config_until_is_optional():
    cfg = LoopNodeConfig.parse(_cfg())
    assert cfg.until is None
    cfg2 = LoopNodeConfig.parse(_cfg(until={"left": {"path": "loop.iteration"}, "op": "gte", "right": 3}))
    assert cfg2.until is not None and cfg2.until.right == 3


def test_config_checks_body_against_known_sibling_ids_when_given():
    with pytest.raises(ContractError):
        LoopNodeConfig.parse(_cfg(), known_node_ids=["draft"])  # "review" is unknown
    ok = LoopNodeConfig.parse(_cfg(), known_node_ids=["draft", "review", "loop"])
    assert ok.body == ("draft", "review")


def test_config_skips_the_cross_reference_when_known_node_ids_is_omitted():
    cfg = LoopNodeConfig.parse(_cfg(body=["nowhere"]))
    assert cfg.body == ("nowhere",)


def test_config_round_trips():
    cfg = LoopNodeConfig.parse(_cfg())
    d = cfg.to_dict()
    assert d["body"] == ["draft", "review"]
    assert d["budget"]["max_iterations"] == 4
    assert d["until"] is None
    assert d["idempotency_scope"] == "effect_and_iteration"


# ── IterationState ───────────────────────────────────────────────────────────

def test_iteration_state_requires_ended_at_once_terminal():
    with pytest.raises(ContractError):
        IterationState.parse({"workflow_run_id": "r1", "node_id": "loop", "iteration": 1,
                              "status": "completed"})
    ok = IterationState.parse({"workflow_run_id": "r1", "node_id": "loop", "iteration": 1,
                               "status": "completed", "ended_at": "2026-09-11T00:00:00Z"})
    assert ok.status == "completed"


def test_iteration_state_defaults_to_pending():
    s = IterationState.parse({"workflow_run_id": "r1", "node_id": "loop", "iteration": 2})
    assert s.status == "pending" and s.iteration == 2


def test_iteration_state_round_trips():
    s = IterationState.parse({"workflow_run_id": "r1", "node_id": "loop", "iteration": 3,
                              "status": "failed", "ended_at": "2026-09-11T00:00:00Z",
                              "reason": "budget", "result": {"draft": "v3"}})
    d = s.to_dict()
    assert d["iteration"] == 3 and d["result"] == {"draft": "v3"} and d["reason"] == "budget"


def test_iteration_state_rejects_a_zero_or_negative_iteration():
    with pytest.raises(ContractError):
        IterationState.parse({"workflow_run_id": "r1", "node_id": "loop", "iteration": 0})


# ── idempotency: per effect, per iteration ───────────────────────────────────

def test_same_iteration_same_config_collides():
    a = loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=2,
                                    config={"skill": "publish"}, inputs={"x": 1})
    b = loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=2,
                                    config={"skill": "publish"}, inputs={"x": 1})
    assert a == b


def test_different_iterations_never_collide():
    a = loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=2,
                                    config={"skill": "publish"})
    b = loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=3,
                                    config={"skill": "publish"})
    assert a != b


def test_different_nodes_or_runs_never_collide():
    base = dict(node_id="loop", iteration=1, config={"skill": "publish"})
    a = loop_effect_idempotency_key(workflow_run_id="r1", **base)
    b = loop_effect_idempotency_key(workflow_run_id="r2", **base)
    c = loop_effect_idempotency_key(workflow_run_id="r1", node_id="other", iteration=1,
                                    config={"skill": "publish"})
    assert len({a, b, c}) == 3


def test_iteration_must_be_a_positive_int_not_a_bool_or_zero():
    with pytest.raises(ValueError):
        loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=0, config={})
    with pytest.raises(ValueError):
        loop_effect_idempotency_key(workflow_run_id="r1", node_id="loop", iteration=True, config={})
