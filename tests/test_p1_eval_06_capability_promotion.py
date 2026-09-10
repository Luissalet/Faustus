"""
tests/test_p1_eval_06_capability_promotion.py — EVAL-06, lote 53.

Acceptance line: "Un modelo textual no hereda soporte de vision de otro con
nombre parecido ni un parser antiguo conserva sello compatible tras
romperse." Both halves pinned against `src/capability_promotion.py`.
"""
from __future__ import annotations

import pytest

from src.capability_promotion import CapabilityMatrix, PromotionRefused


def test_a_similarly_named_model_does_not_inherit_another_ones_state():
    matrix = CapabilityMatrix()
    matrix.record_calibration("gpt-4-vision", "openai", "vision-default", passed=True)
    matrix.promote("gpt-4-vision", "openai", "vision-default",
                   to_state="experimental", flag_enabled=True)

    lookalike = matrix.get("gpt-4-vision-preview", "openai", "vision-default")
    assert lookalike.state == "not_tested"
    assert lookalike.history == []


def test_a_parser_that_breaks_loses_its_compatible_seal():
    matrix = CapabilityMatrix()
    matrix.record_calibration("claude-x", "local", "table-parser-v1", passed=True)
    matrix.promote("claude-x", "local", "table-parser-v1", to_state="experimental", flag_enabled=True)
    matrix.record_calibration("claude-x", "local", "table-parser-v1", passed=True)
    matrix.promote("claude-x", "local", "table-parser-v1", to_state="compatible", flag_enabled=True)
    entry = matrix.get("claude-x", "local", "table-parser-v1")
    assert entry.state == "compatible"

    # The parser regresses — a later calibration fails.
    matrix.record_calibration("claude-x", "local", "table-parser-v1", passed=False,
                              details="new PDF layout breaks column detection")
    entry = matrix.get("claude-x", "local", "table-parser-v1")
    assert entry.state != "compatible"
    assert entry.state == "experimental"       # demoted, not left at the stale seal


def test_promotion_without_the_flag_is_refused():
    matrix = CapabilityMatrix()
    matrix.record_calibration("m", "b", "t", passed=True)
    with pytest.raises(PromotionRefused):
        matrix.promote("m", "b", "t", to_state="experimental", flag_enabled=False)
    assert matrix.get("m", "b", "t").state == "not_tested"


def test_promotion_without_a_passing_calibration_is_refused():
    matrix = CapabilityMatrix()
    matrix.record_calibration("m", "b", "t", passed=False)
    with pytest.raises(PromotionRefused):
        matrix.promote("m", "b", "t", to_state="experimental", flag_enabled=True)


def test_promotion_cannot_skip_a_state():
    matrix = CapabilityMatrix()
    matrix.record_calibration("m", "b", "t", passed=True)
    with pytest.raises(PromotionRefused):
        matrix.promote("m", "b", "t", to_state="compatible", flag_enabled=True)  # skips experimental


def test_evidence_must_be_the_most_recent_run_not_any_passing_run_ever():
    matrix = CapabilityMatrix()
    matrix.record_calibration("m", "b", "t", passed=True)     # old pass
    matrix.record_calibration("m", "b", "t", passed=False)    # then it broke
    with pytest.raises(PromotionRefused):
        matrix.promote("m", "b", "t", to_state="experimental", flag_enabled=True)


def test_the_full_ladder_one_step_at_a_time():
    matrix = CapabilityMatrix()
    for target in ("experimental", "compatible", "recommended"):
        matrix.record_calibration("m2", "b2", "t2", passed=True)
        matrix.promote("m2", "b2", "t2", to_state=target, flag_enabled=True)
    entry = matrix.get("m2", "b2", "t2")
    assert entry.state == "recommended"
    assert [p["to"] for p in entry.promotions] == ["experimental", "compatible", "recommended"]


def test_a_key_with_the_reserved_separator_is_rejected_rather_than_silently_colliding():
    matrix = CapabilityMatrix()
    with pytest.raises(ValueError):
        matrix.record_calibration("model\x1fname", "b", "t", passed=True)
