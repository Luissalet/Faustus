"""tests/test_cmp09_strategy.py — CMP-09/CMP-12 (W2-F, CONTRATO_CMP_W2.md).

`src.strategy_policy` (an observable, editable `Strategy` per turn),
`src.recipes` (structured procedures, plus drafting one from a finished
run), and the transport (`routes/strategy_routes.py`).

The decisive test the ficha names: same corpus as `tests/eval/tasks.py`,
varying only the strategy — `direct_edit` must come out cheaper than
`deep_review` for a small task, and the policy must never default to a
"council"/multi-agent method.

No network calls anywhere in this file.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import recipes  # noqa: E402
from src import strategy_policy  # noqa: E402
from tests.eval import tasks as eval_tasks  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# choose_strategy — method classification
# ---------------------------------------------------------------------------

def test_never_defaults_to_council():
    """CMP-09's own decisive requirement: no branch of `choose_strategy` can
    ever produce a method outside `METHODS` — "council" is not in it at all,
    so this holds by construction, not by a runtime check. Exercised across
    every real task message from tests/eval/tasks.py plus the profiles, so
    the assertion is not just "the set has no council in it" but "nothing
    this function actually returns for real input is outside it either"."""
    assert "council" not in strategy_policy.METHODS
    for task in (eval_tasks.BUG_FIX, eval_tasks.FEATURE, eval_tasks.REFACTOR,
                 eval_tasks.INVESTIGATION, eval_tasks.DOCUMENT, eval_tasks.TABULAR):
        for profile in strategy_policy.PROFILES:
            strategy = strategy_policy.choose_strategy(task.message, profile=profile,
                                                        context={"hardware_aware": False})
            assert strategy.method in strategy_policy.METHODS
            assert strategy.method != "council"


def test_direct_edit_pattern_wins_for_a_short_edit_task():
    strategy = strategy_policy.choose_strategy(
        "Corrige este pasaje sin cambiar el tono: hay un typo en la segunda línea.",
        profile="balanced", context={"hardware_aware": False},
    )
    assert strategy.method == "direct_edit"
    assert strategy.reasons  # never a bare method with no reason


def test_review_pattern_wins_for_a_review_task():
    strategy = strategy_policy.choose_strategy(
        "Revisa estos cambios antes de fusionarlos, el PR toca tres ficheros.",
        profile="balanced", context={"hardware_aware": False},
    )
    assert strategy.method == "specialised_review"


def test_research_pattern_wins_for_sources_to_report():
    strategy = strategy_policy.choose_strategy(
        "Convierte estas fuentes en un informe con las citas correspondientes.",
        profile="balanced", context={"hardware_aware": False},
    )
    assert strategy.method == "research"


def test_escalation_only_from_observable_signals_never_from_confidence():
    base = strategy_policy.choose_strategy(
        "Corrige este texto sin cambiar el tono.", profile="balanced",
        context={"hardware_aware": False},
    )
    assert base.method == "direct_edit"

    # A bare "context" with no observable signal at all changes nothing.
    unchanged = strategy_policy.choose_strategy(
        "Corrige este texto sin cambiar el tono.", profile="balanced",
        context={"hardware_aware": False, "model_confidence": "low"},
    )
    assert unchanged.method == "direct_edit"

    # An OBSERVED failure is the only thing that escalates it.
    escalated = strategy_policy.choose_strategy(
        "Corrige este texto sin cambiar el tono.", profile="balanced",
        context={"hardware_aware": False, "failures_observed": ["test_calc.py::test_add failed"]},
    )
    assert escalated.method == "plan_then_execute"
    assert any("escalated" in r for r in escalated.reasons)


# ---------------------------------------------------------------------------
# Decisive test: direct_edit is cheaper than deep_review for a small task
# ---------------------------------------------------------------------------

def test_direct_edit_is_cheaper_than_deep_review_for_a_small_task():
    """The exact assertion the ficha names: for a small task, direct_edit's
    own steps/calls come out lower than deep_review's — and the comparison
    is fair (same task text, same method, only the profile differs), which
    is why deep_review here is asked on the SAME direct_edit-shaped task
    text rather than a task that would pick a different method on its own."""
    task_text = eval_tasks.BUG_FIX.message  # a small, single-file bug fix
    fast = strategy_policy.choose_strategy(task_text, profile="fast", context={"hardware_aware": False})
    deep = strategy_policy.choose_strategy(task_text, profile="deep_review", context={"hardware_aware": False})
    assert fast.method == deep.method == "direct_edit"
    assert len(fast.steps) < len(deep.steps)
    assert fast.budget["calls"] < deep.budget["calls"]
    assert fast.budget["tokens"] < deep.budget["tokens"]
    assert any("review" in r.lower() for r in deep.reasons)


def test_profile_scales_budget_monotonically_for_the_same_method():
    task_text = eval_tasks.BUG_FIX.message
    by_profile = {
        p: strategy_policy.choose_strategy(task_text, profile=p, context={"hardware_aware": False})
        for p in strategy_policy.PROFILES
    }
    assert by_profile["fast"].budget["calls"] <= by_profile["balanced"].budget["calls"]
    assert by_profile["balanced"].budget["calls"] <= by_profile["deep_review"].budget["calls"]


def test_profile_diff_is_pure_and_reports_the_review_step():
    diff = strategy_policy.profile_diff("balanced", "deep_review")
    assert diff["adds_review_step"] is True
    assert diff["drops_review_step"] is False
    reverse = strategy_policy.profile_diff("deep_review", "fast")
    assert reverse["drops_review_step"] is True


# ---------------------------------------------------------------------------
# Recipe substitution
# ---------------------------------------------------------------------------

def test_active_recipe_replaces_generic_steps():
    strategy = strategy_policy.choose_strategy(
        "Corrige este texto sin cambiar el tono.", profile="balanced",
        context={"hardware_aware": False, "recipe_id": "edit-passage-keep-tone"},
    )
    recipe = recipes.get_recipe("edit-passage-keep-tone")
    assert recipe is not None
    assert strategy.steps == recipe.steps
    assert any("recipe" in r for r in strategy.reasons)


def test_unknown_recipe_id_falls_back_to_generic_steps_and_says_so():
    strategy = strategy_policy.choose_strategy(
        "Corrige este texto sin cambiar el tono.", profile="balanced",
        context={"hardware_aware": False, "recipe_id": "does-not-exist"},
    )
    assert strategy.method == "direct_edit"
    assert strategy.steps == strategy_policy._BASE_STEPS["direct_edit"]
    assert any("not found" in r for r in strategy.reasons)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_get_active_defaults_when_nothing_set():
    active = strategy_policy.get_active("alice")
    assert active == {"profile": strategy_policy.DEFAULT_PROFILE, "recipe_id": None}


def test_set_active_round_trips_and_is_owner_scoped():
    strategy_policy.set_active("alice", profile="deep_review", recipe_id="review-changes")
    assert strategy_policy.get_active("alice") == {"profile": "deep_review", "recipe_id": "review-changes"}
    assert strategy_policy.get_active("bob") == {"profile": strategy_policy.DEFAULT_PROFILE, "recipe_id": None}


def test_session_scope_overrides_owner_default():
    strategy_policy.set_active("alice", profile="fast")
    strategy_policy.set_active("alice", session_id="s1", profile="deep_review")
    assert strategy_policy.get_active("alice") == {"profile": "fast", "recipe_id": None}
    assert strategy_policy.get_active("alice", "s1") == {"profile": "deep_review", "recipe_id": None}
    assert strategy_policy.get_active("alice", "s2") == {"profile": "fast", "recipe_id": None}


def test_set_active_rejects_unknown_profile():
    with pytest.raises(ValueError):
        strategy_policy.set_active("alice", profile="ludicrous_speed")


def test_clearing_recipe_id_with_empty_string():
    strategy_policy.set_active("alice", recipe_id="review-changes")
    assert strategy_policy.get_active("alice")["recipe_id"] == "review-changes"
    strategy_policy.set_active("alice", recipe_id="")
    assert strategy_policy.get_active("alice")["recipe_id"] is None


# ---------------------------------------------------------------------------
# Recipes — built-ins
# ---------------------------------------------------------------------------

def test_four_builtin_recipes_are_present_and_well_formed():
    built_ins = recipes.list_recipes()
    ids = {r.id for r in built_ins}
    assert ids == {
        "review-changes", "sources-to-report", "design-function-and-tests", "edit-passage-keep-tone",
    }
    for recipe in built_ins:
        assert recipe.status == "published"
        assert recipe.title
        assert recipe.steps
        assert recipe.success_conditions


def test_drafts_are_private_per_owner():
    assert recipes.list_recipes(owner=None) == recipes.list_recipes()
    assert recipes.list_recipes(owner="alice") == recipes.list_recipes()  # no drafts yet


# ---------------------------------------------------------------------------
# from_run
# ---------------------------------------------------------------------------

def _write_run_log(tmp_path, run_id, *, finished=True, label="Fix the off-by-one", secret=None):
    runs_dir = os.path.join(str(tmp_path), "runs")
    os.makedirs(runs_dir, exist_ok=True)
    lines = [
        {"status": "running", "run_id": run_id, "session_id": run_id, "lane": "local", "label": label},
        {"seq": 1, "ev": 'data: {"type": "tool_start", "tool_type": "read_file"}\n\n'},
        {"seq": 2, "ev": 'data: {"type": "tool_output", "tool_type": "read_file"}\n\n'},
        {"seq": 3, "ev": 'data: {"type": "tool_start", "tool_type": "edit_file"}\n\n'},
    ]
    if secret:
        lines.append({"seq": 4, "ev": f'data: {{"type": "tool_output", "content": "api_key: {secret}"}}\n\n'})
    if finished:
        lines.append({"status": "done", "ts": 1.0})
    else:
        lines.append({"status": "stopped", "ts": 1.0})
    path = os.path.join(runs_dir, f"{run_id}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def test_from_run_builds_a_draft_with_distinct_tool_steps(tmp_path):
    _write_run_log(tmp_path, "run-1")
    recipe = recipes.from_run("run-1", "alice")
    assert recipe.status == "draft"
    assert recipe.title == "Fix the off-by-one"
    assert recipe.tools == ["read_file", "edit_file"]
    assert recipe.steps == ["use `read_file`", "use `edit_file`"]
    assert recipe.source["run_id"] == "run-1"
    assert recipe.source["owner"] == "alice"
    # persisted as a draft, and only visible to its owner
    assert recipe.id in {r.id for r in recipes.list_recipes(owner="alice")}
    assert recipe.id not in {r.id for r in recipes.list_recipes(owner="bob")}


def test_from_run_redacts_secrets_before_anything_is_extracted(tmp_path):
    _write_run_log(tmp_path, "run-secret", secret="sk-super-secret-value")
    recipe = recipes.from_run("run-secret", "alice")
    dumped = json.dumps(recipe.to_dict())
    assert "sk-super-secret-value" not in dumped


def test_from_run_rejects_an_unfinished_run(tmp_path):
    _write_run_log(tmp_path, "run-2", finished=False)
    with pytest.raises(recipes.RecipeFromRunError) as exc:
        recipes.from_run("run-2", "alice")
    assert exc.value.error_class == "recipes.run_not_finished"


def test_from_run_rejects_a_missing_run(tmp_path):
    with pytest.raises(recipes.RecipeFromRunError) as exc:
        recipes.from_run("does-not-exist", "alice")
    assert exc.value.error_class == "recipes.run_not_found"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

import routes.strategy_routes as sr  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(sr.setup_strategy_routes())
    return TestClient(app)


def test_get_then_put_strategy_profile_route(client):
    resp = client.get("/api/strategy/profile")
    assert resp.status_code == 200
    assert resp.json()["profile"] == strategy_policy.DEFAULT_PROFILE

    resp = client.put("/api/strategy/profile", json={"profile": "deep_review"})
    assert resp.status_code == 200
    assert resp.json()["profile"] == "deep_review"

    resp = client.get("/api/strategy/profile")
    assert resp.json()["profile"] == "deep_review"


def test_put_strategy_profile_rejects_unknown_profile(client):
    resp = client.put("/api/strategy/profile", json={"profile": "nonsense"})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "strategy.invalid_profile"


def test_preview_route_returns_a_strategy_and_optional_diff(client):
    resp = client.post("/api/strategy/preview", json={
        "task_text": eval_tasks.BUG_FIX.message, "profile": "fast", "compare_profile": "deep_review",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"]["method"] == "direct_edit"
    assert body["diff"]["adds_review_step"] is True


def test_preview_route_requires_task_text(client):
    resp = client.post("/api/strategy/preview", json={"profile": "fast"})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "strategy.missing_task_text"


def test_list_recipes_route(client):
    resp = client.get("/api/recipes")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["recipes"]}
    assert "review-changes" in ids


def test_recipe_from_run_route(client, tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    _write_run_log(tmp_path, "run-http")
    resp = client.post("/api/recipes/from-run/run-http")
    assert resp.status_code == 200
    body = resp.json()["recipe"]
    assert body["status"] == "draft"
    assert body["tools"] == ["read_file", "edit_file"]


def test_recipe_from_run_route_missing_run_is_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    resp = client.post("/api/recipes/from-run/nope")
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "recipes.run_not_found"
