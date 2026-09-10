"""The §04 task state machine (`assert_transition`), `PlanRevision`'s two
concurrency guards, and the extra checks `task.py`/`errors.py` apply beyond
what a bare JSON Schema can express (a recommended option that names no real
option, an OBS-03 taxonomy that classifies the repo's own exceptions).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.exceptions import InvalidFileUploadError, SessionNotFoundError
from src.contracts import ContractError
from src.contracts.errors import ErrorInfo, from_exception
from src.contracts.task import (
    TASK_STATES, TASK_TERMINAL, PlanRevision, QuestionRequest, TaskState, assert_transition,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "spec" / "v2" / "examples"


def _task_state_example():
    with open(EXAMPLES / "task_state.valid.json", encoding="utf-8") as f:
        return json.load(f)


def _question_example():
    with open(EXAMPLES / "question_request.valid.json", encoding="utf-8") as f:
        return json.load(f)


# ── assert_transition ────────────────────────────────────────────────────────

def test_a_legal_transition_is_silent():
    assert_transition("draft", "ready")
    assert_transition("running", "waiting_approval")
    assert_transition("waiting_approval", "running")


def test_staying_put_is_always_allowed():
    for state in TASK_STATES:
        assert_transition(state, state)


def test_a_terminal_task_does_not_change_its_mind():
    for terminal in TASK_TERMINAL:
        with pytest.raises(ContractError) as err:
            assert_transition(terminal, "running")
        assert "terminal" in str(err.value)


def test_an_illegal_but_nonterminal_jump_names_what_was_allowed():
    with pytest.raises(ContractError) as err:
        assert_transition("draft", "succeeded")
    assert "allowed from draft" in str(err.value)


def test_an_unknown_state_name_is_refused_on_either_side():
    with pytest.raises(ContractError):
        assert_transition("draft", "magically_verified")
    with pytest.raises(ContractError):
        assert_transition("magically_verified", "draft")


def test_waiting_approval_may_fail_directly_when_denied():
    # A denied approval does not have to bounce back through `running` first.
    assert_transition("waiting_approval", "failed")


# ── PlanRevision ──────────────────────────────────────────────────────────────

def test_plan_revision_advances_on_a_legal_forward_move():
    base = PlanRevision.from_mapping(_task_state_example())  # revision 3, state "running"
    nxt = base.advance({**_task_state_example(), "revision": 4, "state": "verifying"})
    assert nxt.task.revision == 4
    assert nxt.task.state == "verifying"


def test_plan_revision_refuses_a_revision_that_does_not_move_forward():
    base = PlanRevision.from_mapping(_task_state_example())
    with pytest.raises(ContractError) as err:
        base.advance({**_task_state_example(), "revision": 3, "state": "verifying"})
    assert err.value.path == "task_state.revision"
    with pytest.raises(ContractError):
        base.advance({**_task_state_example(), "revision": 2, "state": "verifying"})


def test_plan_revision_refuses_an_illegal_state_transition_even_with_a_higher_revision():
    base = PlanRevision.from_mapping(_task_state_example())  # state "running"
    with pytest.raises(ContractError) as err:
        base.advance({**_task_state_example(), "revision": 99, "state": "draft"})
    assert err.value.path == "task_state.state"


def test_plan_revision_refuses_a_snapshot_of_a_different_task():
    base = PlanRevision.from_mapping(_task_state_example())
    with pytest.raises(ContractError) as err:
        base.advance({**_task_state_example(), "task_id": "some_other_task", "revision": 4})
    assert err.value.path == "task_state.task_id"


# ── QuestionRequest's extra invariant beyond the bare schema ─────────────────

def test_a_recommended_option_must_be_a_real_option():
    raw = _question_example()
    with pytest.raises(ContractError) as err:
        QuestionRequest.from_mapping({**raw, "recommended_option_id": "not_an_option"})
    assert err.value.path == "question_request.recommended_option_id"


def test_a_recommended_option_of_null_is_fine():
    raw = _question_example()
    q = QuestionRequest.from_mapping({**raw, "recommended_option_id": None})
    assert q.recommended_option_id is None


# ── errors.py: the OBS-03 taxonomy ───────────────────────────────────────────

def test_from_exception_classifies_a_registered_exception():
    info = from_exception(SessionNotFoundError("sess1"))
    assert info.code == "resource.not_found"
    assert info.retryable is False
    assert "sess1" in info.message


def test_from_exception_classifies_contracterror_as_schema():
    bad = None
    try:
        TaskState.from_mapping({"not": "a task"})
    except ContractError as e:
        bad = e
    assert bad is not None
    info = from_exception(bad)
    assert info.code == "schema.contract_violation"


def test_an_unregistered_exception_says_so_instead_of_guessing():
    info = from_exception(RuntimeError("something this map has never seen"))
    assert info.code == "unknown.unmapped_exception"


def test_transport_errors_default_to_retryable():
    info = from_exception(InvalidFileUploadError("bad file"))
    assert info.code == "schema.invalid_upload"
    assert info.retryable is False


def test_a_caller_can_override_the_default_next_action_and_retryable():
    info = from_exception(SessionNotFoundError("s1"), next_action="ask_user_to_relogin",
                          retryable=True)
    assert info.next_action == "ask_user_to_relogin"
    assert info.retryable is True


def test_error_info_round_trips_through_mapping():
    info = ErrorInfo(code="conflict.base_revision_mismatch", message="changed underfoot",
                     retryable=False, next_action="read_current_and_reconcile")
    again = ErrorInfo.from_mapping(info.to_mapping())
    assert again == info


def test_error_info_code_must_be_an_identifier_not_arbitrary_text():
    with pytest.raises(ContractError) as err:
        ErrorInfo.from_mapping({"code": "not a valid code!", "message": "x",
                                "retryable": False, "next_action": "y"})
    assert err.value.path == "error.code"


def test_error_info_can_require_a_known_category_when_asked():
    with pytest.raises(ContractError) as err:
        ErrorInfo.from_mapping(
            {"code": "TOTALLY_MADE_UP", "message": "x", "retryable": False, "next_action": "y"},
            require_known_category=True)
    assert err.value.path == "error.code"
    # The same payload is fine without that flag — a tool's own vocabulary.
    ok = ErrorInfo.from_mapping(
        {"code": "TOTALLY_MADE_UP", "message": "x", "retryable": False, "next_action": "y"})
    assert ok.code == "TOTALLY_MADE_UP"
