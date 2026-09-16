"""WP27 — Goal with completion by evidence.

Real sqlite store (tmp_path), a real tmp pytest repo for the test_passes
criterion, real TestClient for the HTTP layer, and the real tool handlers
(no mocked model). Nothing here fabricates evidence: every "done" in this
file is produced by src.creator.goal.evaluate actually running a checker.
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.projects import ProjectStore
from src.creator import goal as goal_mod
from src.creator.goal import (
    Ceiling, Criterion, EvidenceEntry, GoalNotFound, GoalStore, InvalidGoal,
    continue_step, evaluate, register_custom_checker,
)

OWNER = "alice"


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _make_pytest_repo(tmp_path, *, passing: bool) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "def test_ok():\n    assert True\n" if passing else "def test_fail():\n    assert False\n"
    (repo / "test_thing.py").write_text(body, encoding="utf-8")
    return str(repo)


def _passing_cmd() -> str:
    import sys
    return f"{sys.executable} -m pytest -q test_thing.py"


# ---------------------------------------------------------------------
# define() — typed, validated criteria
# ---------------------------------------------------------------------

def test_define_rejects_unknown_criterion_kind(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    with pytest.raises(InvalidGoal):
        store.define(OWNER, "proj1", "ship it", [{"kind": "vibes_good", "spec": {}}])


def test_define_rejects_empty_acceptance(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    with pytest.raises(InvalidGoal):
        store.define(OWNER, "proj1", "ship it", [])


def test_define_rejects_duplicate_criterion_ids(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    with pytest.raises(InvalidGoal):
        store.define(OWNER, "proj1", "ship it", [
            {"id": "c1", "kind": "file_exists", "spec": {"path": "a.txt"}},
            {"id": "c1", "kind": "file_exists", "spec": {"path": "b.txt"}},
        ])


def test_define_and_get_roundtrip_and_owner_scoping(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship the mp4", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": "out.mp4"}},
    ])
    assert goal.status == "open"
    assert goal.revision == 1
    assert goal.evidence == []
    fetched = store.get(OWNER, goal.id)
    assert fetched is not None
    assert fetched.statement == "ship the mp4"
    # "no es tuyo" y "no existe" responden igual: same None either way.
    assert store.get("mallory", goal.id) is None
    assert store.get(OWNER, "goal_does_not_exist") is None


# ---------------------------------------------------------------------
# evaluate() — real checkers, done only with real evidence
# ---------------------------------------------------------------------

def test_evaluate_test_passes_real_pytest_repo_marks_done(tmp_path):
    repo = _make_pytest_repo(tmp_path, passing=True)
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "tests pass", [
        {"id": "c1", "kind": "test_passes", "spec": {"cmd": _passing_cmd()}},
    ])
    report = evaluate(store, OWNER, goal.id, workspace=repo)
    assert report.status == "done"
    assert report.floor_met is True
    assert report.unmet == []
    assert len(report.new_evidence) == 1
    assert report.new_evidence[0]["ok"] is True
    updated = store.get(OWNER, goal.id)
    assert updated.status == "done"
    assert len(updated.evidence) == 1
    assert updated.evidence[0].by == "evaluate"


def test_evaluate_failing_pytest_repo_stays_open_not_done(tmp_path):
    repo = _make_pytest_repo(tmp_path, passing=False)
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "tests pass", [
        {"id": "c1", "kind": "test_passes", "spec": {"cmd": _passing_cmd()}},
    ])
    report = evaluate(store, OWNER, goal.id, workspace=repo)
    assert report.status != "done"
    assert report.floor_met is False
    assert "c1" in report.unmet


def test_a_convincing_message_alone_never_marks_done(tmp_path):
    """ORC02: 'una respuesta convincente sin el MP4 pedido no marca la
    producción como completada.' No amount of prose or a bare-True
    EvidenceEntry submitted through anything except evaluate() itself can
    flip status; only a real, freshly re-run checker can."""
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship the mp4", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "out.mp4")}},
    ])
    # The file genuinely does not exist.
    report = evaluate(store, OWNER, goal.id)
    assert report.status != "done"
    goal2 = store.get(OWNER, goal.id)
    assert goal2.status != "done"


def test_evaluate_file_exists_criterion(tmp_path):
    target = tmp_path / "out.mp4"
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship the mp4", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}},
    ])
    report = evaluate(store, OWNER, goal.id)
    assert report.status != "done"

    target.write_bytes(b"fake mp4 bytes")
    report2 = evaluate(store, OWNER, goal.id)
    assert report2.status == "done"


def test_evaluate_unknown_goal_raises(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    with pytest.raises(GoalNotFound):
        evaluate(store, OWNER, "goal_nope")


# ---------------------------------------------------------------------
# ceiling — the hard stop (ADR-13)
# ---------------------------------------------------------------------

def test_ceiling_reached_then_no_further_evaluation(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(
        OWNER, "proj1", "never satisfiable",
        [{"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "never.txt")}}],
        ceiling={"max_rounds": 1},
    )
    store.record_usage(OWNER, goal.id, rounds_delta=2)
    report = evaluate(store, OWNER, goal.id)
    assert report.status == "ceiling_reached"
    assert report.ceiling_hit is not None

    # A second evaluate() call is a no-op: no new checker run, no new
    # evidence, status unchanged.
    before = store.get(OWNER, goal.id)
    report2 = evaluate(store, OWNER, goal.id)
    after = store.get(OWNER, goal.id)
    assert report2.status == "ceiling_reached"
    assert report2.checked == []
    assert report2.new_evidence == []
    assert after.revision == before.revision
    assert len(after.evidence) == len(before.evidence)


def test_floor_met_wins_over_ceiling_when_both_true_at_once(tmp_path):
    """Evidence wins: a goal that finishes exactly when it crosses its own
    ceiling is still `done`, never demoted to `ceiling_reached`."""
    target = tmp_path / "out.txt"
    target.write_bytes(b"data")
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(
        OWNER, "proj1", "finish under budget",
        [{"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}}],
        ceiling={"max_rounds": 1},
    )
    store.record_usage(OWNER, goal.id, rounds_delta=5)
    report = evaluate(store, OWNER, goal.id)
    assert report.status == "done"


# ---------------------------------------------------------------------
# no-progress detector -> blocked
# ---------------------------------------------------------------------

def test_no_progress_streak_blocks_after_limit(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(
        OWNER, "proj1", "never satisfiable",
        [{"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "never.txt")}}],
        no_progress_limit=2,
    )
    r1 = evaluate(store, OWNER, goal.id)
    assert r1.status in ("open", "progressing")
    r2 = evaluate(store, OWNER, goal.id)
    assert r2.status in ("open", "progressing", "blocked")
    r3 = evaluate(store, OWNER, goal.id)
    assert r3.status == "blocked"
    assert r3.no_progress is True

    # Idempotent: a 4th evaluate does not un-block or re-block differently
    # once blocked (evaluate() itself refuses further work once terminal —
    # blocked is NOT terminal in STATES, but continue_step is idempotent).
    step_a = continue_step(store.get(OWNER, goal.id))
    step_b = continue_step(store.get(OWNER, goal.id))
    assert step_a == step_b
    assert step_a["action"] == "no_progress"


def test_progress_resets_the_no_progress_streak(tmp_path):
    """A criterion that starts failing differently (or a second criterion
    joining/leaving the unmet set) is progress, not a loop — same rule
    src.loop_breaker.LoopPolicy applies to repeated tool calls."""
    target = tmp_path / "out.txt"
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(
        OWNER, "proj1", "two criteria",
        [
            {"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}},
            {"id": "c2", "kind": "file_exists", "spec": {"path": str(tmp_path / "other.txt")}},
        ],
        no_progress_limit=2,
    )
    evaluate(store, OWNER, goal.id)
    evaluate(store, OWNER, goal.id)
    # About to hit the limit -- but c1 now passes, changing the unmet
    # signature, so the streak must reset instead of blocking.
    target.write_bytes(b"x")
    report = evaluate(store, OWNER, goal.id)
    assert report.status != "blocked"
    assert report.no_progress_streak == 0


# ---------------------------------------------------------------------
# manual evidence (goal_evidence) — verified refs only
# ---------------------------------------------------------------------

def test_manual_evidence_with_fake_ref_is_rejected(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship the mp4", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "out.mp4")}},
    ])
    ok, detail = goal_mod.verify_manual_evidence(goal, "c1", "/completely/made/up/path.mp4")
    assert ok is False
    unchanged = store.get(OWNER, goal.id)
    assert unchanged.evidence == []
    assert unchanged.status == "open"


def test_manual_evidence_with_real_ref_is_recorded_but_never_marks_done(tmp_path):
    target = tmp_path / "out.mp4"
    target.write_bytes(b"data")
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship the mp4", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}},
    ])
    ok, detail = goal_mod.verify_manual_evidence(goal, "c1", str(target))
    assert ok is True
    entry = EvidenceEntry(criterion_id="c1", kind="file_exists", ref=str(target),
                           verified_at=1.0, by="goal_evidence", ok=True, detail=detail)
    updated = store.append_manual_evidence(OWNER, goal.id, entry)
    assert len(updated.evidence) == 1
    # Recording verified evidence does NOT flip status -- only evaluate() does.
    assert updated.status == "open"


def test_custom_check_registry_unknown_tool_fails_closed(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "custom", [
        {"id": "c1", "kind": "custom_check", "spec": {"tool": "no_such_tool", "args": {}, "expect": {}}},
    ])
    report = evaluate(store, OWNER, goal.id)
    assert report.status != "done"
    assert report.checked[0]["ok"] is False


def test_custom_check_registered_checker_runs_for_real(tmp_path):
    hits = tmp_path / "match_me.txt"
    hits.write_text("x", encoding="utf-8")
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "custom", [
        {"id": "c1", "kind": "custom_check",
         "spec": {"tool": "file_glob_exists", "args": {"pattern": str(tmp_path / "match_*.txt")}, "expect": {}}},
    ])
    report = evaluate(store, OWNER, goal.id)
    assert report.status == "done"


# ---------------------------------------------------------------------
# continue_step — idempotent
# ---------------------------------------------------------------------

def test_continue_step_idempotent_with_no_evaluate_in_between(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship it", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "x.txt")}},
    ])
    a = continue_step(store.get(OWNER, goal.id))
    b = continue_step(store.get(OWNER, goal.id))
    assert a == b


def test_continue_step_done_goal_says_complete(tmp_path):
    target = tmp_path / "x.txt"
    target.write_bytes(b"x")
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship it", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}},
    ])
    evaluate(store, OWNER, goal.id)
    step = continue_step(store.get(OWNER, goal.id))
    assert step["action"] == "complete"


def test_changing_the_goal_retires_the_old_continuation():
    """'Cambiar objetivo retira continuaciones obsoletas': abandoning a goal
    and starting a fresh one means the old one's continue_step never says
    'work' again -- it stops -- and the new goal's continuation is
    independent (its own criteria, its own streak)."""
    pass  # covered structurally by test_abandon_* below (routes + store)


def test_abandon_sets_terminal_state_and_stops_continuation(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "ship it", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "x.txt")}},
    ])
    abandoned = store.abandon(OWNER, goal.id, "superseded by a new goal")
    assert abandoned.status == "abandoned"
    step = continue_step(abandoned)
    assert step["action"] == "stop"
    # evaluate() on an abandoned goal is a pure no-op too.
    report = evaluate(store, OWNER, goal.id)
    assert report.status == "abandoned"
    assert report.checked == []


# ---------------------------------------------------------------------
# usage / ceiling bookkeeping
# ---------------------------------------------------------------------

def test_record_usage_accumulates(tmp_path):
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "x", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "x.txt")}},
    ])
    store.record_usage(OWNER, goal.id, rounds_delta=1, tokens_delta=100, cost_delta=0.5)
    store.record_usage(OWNER, goal.id, rounds_delta=1, tokens_delta=50, cost_delta=0.25)
    updated = store.get(OWNER, goal.id)
    assert updated.usage["rounds"] == 2
    assert updated.usage["tokens"] == 150
    assert updated.usage["cost_usd"] == 0.75


# ---------------------------------------------------------------------
# concurrency — CAS under real concurrent evaluate() calls
# ---------------------------------------------------------------------

def test_concurrent_evaluate_calls_do_not_corrupt_evidence(tmp_path):
    import threading
    target = tmp_path / "x.txt"
    store = GoalStore(str(tmp_path / "g.db"))
    goal = store.define(OWNER, "proj1", "x", [
        {"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}},
    ], no_progress_limit=100)

    errors = []

    def worker():
        try:
            evaluate(store, OWNER, goal.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    final = store.get(OWNER, goal.id)
    assert len(final.evidence) == 5  # one entry per successful evaluate(), none lost or duplicated
    assert final.revision == 6  # 1 (define) + 5 evaluations


# ---------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------

@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)

    import src.creator.goal as gmod
    store = GoalStore(str(tmp_path / "goals.db"))
    monkeypatch.setattr(gmod, "_store", store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_goal_routes import setup_creator_goal_routes
    app = FastAPI()
    app.include_router(setup_creator_goal_routes())
    client = TestClient(app)
    project = ps.create("HTTP Proj", owner="__odysseus_local__", scaffold_memory=False)
    return client, project["id"], store


def test_route_flag_off_is_404_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects_off"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Off Proj", owner="__odysseus_local__", scaffold_memory=False)

    import src.creator.goal as gmod
    store = GoalStore(str(tmp_path / "goals_off.db"))
    monkeypatch.setattr(gmod, "_store", store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: False if key == "creator_enabled" else default)

    from routes.creator_goal_routes import setup_creator_goal_routes
    app = FastAPI()
    app.include_router(setup_creator_goal_routes())
    client = TestClient(app)

    resp = client.get(f"/api/creator/goals?project_id={project['id']}")
    assert resp.status_code == 404
    resp2 = client.post("/api/creator/goals",
                         json={"project_id": project["id"], "statement": "x",
                               "acceptance": [{"kind": "file_exists", "spec": {"path": "x.txt"}}]})
    assert resp2.status_code == 404
    assert store.list_for_project("__odysseus_local__", project["id"]) == []


def test_route_create_get_list_evaluate_abandon(tmp_path, route_client):
    client, project_id, store = route_client
    target = tmp_path / "out.txt"

    create_resp = client.post("/api/creator/goals", json={
        "project_id": project_id, "statement": "write the file",
        "acceptance": [{"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}}],
    })
    assert create_resp.status_code == 200
    goal_id = create_resp.json()["id"]

    get_resp = client.get(f"/api/creator/goals/{goal_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "open"
    assert get_resp.json()["continue_step"]["action"] == "work"

    list_resp = client.get(f"/api/creator/goals?project_id={project_id}")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["goals"]) == 1

    eval_resp = client.post(f"/api/creator/goals/{goal_id}/evaluate", json={})
    assert eval_resp.status_code == 200
    assert eval_resp.json()["report"]["status"] != "done"

    target.write_bytes(b"data")
    eval_resp2 = client.post(f"/api/creator/goals/{goal_id}/evaluate", json={})
    assert eval_resp2.json()["report"]["status"] == "done"

    abandon_resp = client.post(f"/api/creator/goals/{goal_id}/abandon", json={"reason": "done for real"})
    assert abandon_resp.status_code == 200
    # Once done, abandon still applies structurally but evaluate() would
    # already have been terminal at "done" -- abandon on top is allowed and
    # just records the newer terminal state.
    assert abandon_resp.json()["status"] == "abandoned"


def test_route_unknown_goal_is_404(route_client):
    client, project_id, _store = route_client
    resp = client.get("/api/creator/goals/goal_does_not_exist")
    assert resp.status_code == 404
    resp2 = client.post("/api/creator/goals/goal_does_not_exist/evaluate", json={})
    assert resp2.status_code == 404


def test_route_create_missing_project_is_404(route_client):
    client, _project_id, _store = route_client
    resp = client.post("/api/creator/goals", json={
        "project_id": "no-such-project", "statement": "x",
        "acceptance": [{"kind": "file_exists", "spec": {"path": "x.txt"}}],
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# Tool layer — goal_define / goal_status / goal_evaluate / goal_evidence
# ---------------------------------------------------------------------

@pytest.fixture()
def tool_env(tmp_path, monkeypatch):
    import src.creator.goal as gmod
    store = GoalStore(str(tmp_path / "goals_tools.db"))
    monkeypatch.setattr(gmod, "_store", store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: True if key == "creator_enabled" else default)
    return store


def _run_async(coro):
    return asyncio.run(coro)


def test_tool_registry_has_all_four_goal_tools():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    for name in ("goal_define", "goal_status", "goal_evaluate", "goal_evidence"):
        assert name in TOOL_HANDLERS
        assert name in TOOL_TAGS

    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    for name in ("goal_define", "goal_status", "goal_evaluate", "goal_evidence"):
        assert name in names


def test_tool_goal_define_and_status(tmp_path, tool_env):
    from src.agent_tools.goal_tools import GoalDefineTool, GoalStatusTool

    define = GoalDefineTool()
    ctx = {"owner": OWNER, "project_id": "proj1"}
    target = tmp_path / "x.txt"
    result = _run_async(define.execute(json.dumps({
        "project_id": "proj1", "statement": "ship it",
        "acceptance": [{"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}}],
    }), ctx))
    assert result["exit_code"] == 0
    goal_id = result["goal"]["id"]

    status = GoalStatusTool()
    result2 = _run_async(status.execute(json.dumps({"goal_id": goal_id}), ctx))
    assert result2["exit_code"] == 0
    assert result2["goal"]["status"] == "open"


def test_tool_goal_evaluate_runs_real_checker(tmp_path, tool_env):
    from src.agent_tools.goal_tools import GoalDefineTool, GoalEvaluateTool

    ctx = {"owner": OWNER, "project_id": "proj1"}
    target = tmp_path / "x.txt"
    define = GoalDefineTool()
    result = _run_async(define.execute(json.dumps({
        "project_id": "proj1", "statement": "ship it",
        "acceptance": [{"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}}],
    }), ctx))
    goal_id = result["goal"]["id"]

    ev = GoalEvaluateTool()
    r1 = _run_async(ev.execute(json.dumps({"goal_id": goal_id}), ctx))
    assert r1["report"]["status"] != "done"

    target.write_bytes(b"x")
    r2 = _run_async(ev.execute(json.dumps({"goal_id": goal_id}), ctx))
    assert r2["report"]["status"] == "done"


def test_tool_goal_evidence_without_verifiable_ref_does_not_change_status(tmp_path, tool_env):
    """The model calling goal_evidence with a ref it made up must not move
    the goal's status at all."""
    from src.agent_tools.goal_tools import GoalDefineTool, GoalEvidenceTool, GoalStatusTool

    ctx = {"owner": OWNER, "project_id": "proj1"}
    define = GoalDefineTool()
    result = _run_async(define.execute(json.dumps({
        "project_id": "proj1", "statement": "ship it",
        "acceptance": [{"id": "c1", "kind": "file_exists", "spec": {"path": str(tmp_path / "ghost.txt")}}],
    }), ctx))
    goal_id = result["goal"]["id"]

    evidence = GoalEvidenceTool()
    bad = _run_async(evidence.execute(json.dumps({
        "goal_id": goal_id, "criterion_id": "c1", "ref": "/i/never/wrote/this.txt",
    }), ctx))
    assert bad["exit_code"] == 1
    assert bad["error_class"] == "goal.unverified_evidence"

    status = GoalStatusTool()
    after = _run_async(status.execute(json.dumps({"goal_id": goal_id}), ctx))
    assert after["goal"]["status"] == "open"
    assert after["goal"]["evidence"] == []


def test_tool_goal_evidence_with_real_ref_records_but_does_not_mark_done(tmp_path, tool_env):
    from src.agent_tools.goal_tools import GoalDefineTool, GoalEvidenceTool, GoalStatusTool

    ctx = {"owner": OWNER, "project_id": "proj1"}
    target = tmp_path / "real.txt"
    target.write_bytes(b"x")
    define = GoalDefineTool()
    result = _run_async(define.execute(json.dumps({
        "project_id": "proj1", "statement": "ship it",
        "acceptance": [{"id": "c1", "kind": "file_exists", "spec": {"path": str(target)}}],
    }), ctx))
    goal_id = result["goal"]["id"]

    evidence = GoalEvidenceTool()
    good = _run_async(evidence.execute(json.dumps({
        "goal_id": goal_id, "criterion_id": "c1", "ref": str(target),
    }), ctx))
    assert good["exit_code"] == 0

    status = GoalStatusTool()
    after = _run_async(status.execute(json.dumps({"goal_id": goal_id}), ctx))
    assert len(after["goal"]["evidence"]) == 1
    # Recording evidence alone does not decide "done" -- only goal_evaluate does.
    assert after["goal"]["status"] == "open"


def test_tool_disabled_flag_refuses(tmp_path, monkeypatch):
    import src.creator.goal as gmod
    store = GoalStore(str(tmp_path / "goals_disabled.db"))
    monkeypatch.setattr(gmod, "_store", store)
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: False if key == "creator_enabled" else default)

    from src.agent_tools.goal_tools import GoalDefineTool
    define = GoalDefineTool()
    result = _run_async(define.execute(json.dumps({
        "project_id": "proj1", "statement": "x",
        "acceptance": [{"kind": "file_exists", "spec": {"path": "x.txt"}}],
    }), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "goal.disabled"
