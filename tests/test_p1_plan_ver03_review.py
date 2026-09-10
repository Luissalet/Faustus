"""VER-03 — independent structured review.

Backend requirement: the reviewer is not the same model that wrote the
change, when another is actually available.
"""
from __future__ import annotations

from src import auto_review


def test_same_setting_prefers_a_distinct_model_when_one_is_available(monkeypatch):
    monkeypatch.setattr(auto_review, "_setting", lambda key, default: "same")
    reviewer = auto_review.resolve_reviewer(
        "qwen3-coder", available_models=["qwen3-coder", "gpt-oss-20b"],
    )
    assert reviewer == "gpt-oss-20b"


def test_same_setting_falls_back_to_the_writer_when_nothing_else_is_available(monkeypatch):
    monkeypatch.setattr(auto_review, "_setting", lambda key, default: "same")
    reviewer = auto_review.resolve_reviewer("qwen3-coder", available_models=["qwen3-coder"])
    assert reviewer == "qwen3-coder"


def test_omitting_available_models_keeps_prior_behaviour(monkeypatch):
    """No caller passed `available_models`: `resolve_reviewer` must return
    exactly what it always has — the writer's own model — so no existing
    caller changes behaviour just because this parameter exists."""
    monkeypatch.setattr(auto_review, "_setting", lambda key, default: "same")
    assert auto_review.resolve_reviewer("qwen3-coder") == "qwen3-coder"


def test_explicit_reviewer_model_override_is_unaffected_by_available_models(monkeypatch):
    monkeypatch.setattr(auto_review, "_setting", lambda key, default: "off")
    reviewer = auto_review.resolve_reviewer(
        "qwen3-coder", project_override="gpt-oss-20b",
        available_models=["qwen3-coder", "gpt-oss-20b", "llama-3"],
    )
    assert reviewer == "gpt-oss-20b"


def test_off_setting_still_returns_none():
    reviewer = auto_review.resolve_reviewer(
        "qwen3-coder", project_override="off", available_models=["qwen3-coder", "gpt-oss-20b"],
    )
    assert reviewer is None
