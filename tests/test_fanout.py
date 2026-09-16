"""tests/test_fanout.py — R3 (Reach wave), CONTRATO.md §fan-out.

`src/fanout/` end to end against a real git repo with a real pytest test
file, so scoring is against ACTUAL test results, not a stub. Workers are
faked by monkeypatching `src.fanout.runner._run_subagent` (same seam
`delegate_agents`'s own tests use) -- each candidate writes a different
change into its OWN isolated worktree, one that passes the project's test
and one that breaks it.

Run: python3 -m pytest tests/test_fanout.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import alternatives, budget_account  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src.fanout import merge as fanout_merge  # noqa: E402
from src.fanout import runner as fanout_runner  # noqa: E402
from src.fanout import score as fanout_score  # noqa: E402
from src.fanout import service as fanout_service  # noqa: E402
from src.fanout.plan import FanoutCandidate, FanoutPlan  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    yield


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    """Deterministic settings unless a test overrides one explicitly."""
    overrides = {"agent_fanout_max_parallel": 2, "agent_budget_tokens_per_run": 0}
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: overrides.get(key, default))
    return overrides


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _write(root / "feature.py", "VALUE = False\n")
    _write(root / "test_feature.py", "from feature import VALUE\n\ndef test_it():\n    assert VALUE is True\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "initial"], root)
    return root


def _plan(repo, *, labels=("good", "bad")) -> FanoutPlan:
    return FanoutPlan(
        prompt="Flip VALUE to True in feature.py",
        workspace=str(repo),
        candidates=[FanoutCandidate(label=lbl, model=f"model-{lbl}") for lbl in labels],
        max_rounds=4, budget_tokens=0, project_id="proj1",
    )


def _fake_worker(behavior):
    """Builds a `_run_subagent`-shaped fake: `behavior(run, workspace)` does
    whatever file edit this candidate should make, then the run reports a
    normal completion -- no LLM call, no network."""
    async def fake(run, *, workspace, **kwargs):
        behavior(run, workspace)
        run.tool_calls = 1
        run.input_tokens = 111
        run.output_tokens = 47
        run.stop_reason = "complete"
        run.finished = __import__("time").time()
    return fake


def _good_change(run, workspace):
    _write(os.path.join(workspace, "feature.py"), "VALUE = True\n")


def _bad_change(run, workspace):
    _write(os.path.join(workspace, "feature.py"), "VALUE = False  # still broken\n")


def _crashing_worker(run, workspace):
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# End-to-end: real tests decide the ranking
# ---------------------------------------------------------------------------
def test_fanout_run_all_scores_real_tests_and_ranks_correctly(repo, monkeypatch):
    async def fake(run, *, workspace, **kwargs):
        if run.name == "good":
            _good_change(run, workspace)
        else:
            _bad_change(run, workspace)
        run.tool_calls = 1
        run.input_tokens = 100
        run.output_tokens = 50
        run.stop_reason = "complete"
        import time
        run.finished = time.time()
    monkeypatch.setattr(fanout_runner, "_run_subagent", fake)

    plan = _plan(repo)
    run_id = fanout_runner.start(OWNER, plan)
    manifest = asyncio.run(fanout_runner.run_all(run_id, OWNER))
    assert manifest["status"] == "done"

    candidates = fanout_runner.load_all_candidates(run_id)
    by_label = {c["label"]: c for c in candidates}
    assert by_label["good"]["tests"]["ok"] is True
    assert by_label["bad"]["tests"]["ok"] is False
    assert by_label["good"]["state"] == "done"
    assert by_label["bad"]["state"] == "done"  # the WORKER succeeded; its tests just failed

    results = fanout_service.results(run_id, OWNER)
    assert results["winner"] == "good"
    ranks = {row["label"]: row["rank"] for row in results["ranking"]}
    assert ranks["good"] == 1
    assert ranks["bad"] == 2
    # The reasoning must be able to answer "why did it lose" on its own.
    bad_row = next(r for r in results["ranking"] if r["label"] == "bad")
    assert "fail" in bad_row["reasoning"]


def test_a_crashing_candidate_does_not_sink_the_others(repo, monkeypatch):
    async def fake(run, *, workspace, **kwargs):
        if run.name == "good":
            _good_change(run, workspace)
            run.stop_reason = "complete"
        else:
            _crashing_worker(run, workspace)
        import time
        run.finished = time.time()
    monkeypatch.setattr(fanout_runner, "_run_subagent", fake)

    plan = _plan(repo, labels=("good", "crasher"))
    run_id = fanout_runner.start(OWNER, plan)
    manifest = asyncio.run(fanout_runner.run_all(run_id, OWNER))
    assert manifest["status"] == "done"
    by_label = {c["label"]: c for c in fanout_runner.load_all_candidates(run_id)}
    assert by_label["good"]["state"] == "done"
    assert by_label["crasher"]["state"] == "error"
    assert "boom" in (by_label["crasher"]["error"] or "")

    results = fanout_service.results(run_id, OWNER)
    assert results["winner"] == "good"


# ---------------------------------------------------------------------------
# Checkpoints: resumable after a simulated kill mid-run
# ---------------------------------------------------------------------------
def test_status_reports_a_stuck_candidate_and_resume_finishes_it(repo, monkeypatch):
    plan = _plan(repo, labels=("only",))
    run_id = fanout_runner.start(OWNER, plan)

    # Simulate the process dying mid-run: the candidate's own checkpoint was
    # written as "running" and nothing ever finished it.
    cand = fanout_runner.load_candidate(run_id, "only")
    cand["state"] = "running"
    cand["started_at"] = 1.0
    fanout_runner._save_candidate(run_id, cand)
    manifest = fanout_runner.load_manifest(run_id)
    manifest["status"] = "running"
    fanout_runner._save_manifest(manifest)

    status_before = fanout_service.status(run_id, OWNER)
    assert status_before["status"] == "running"
    assert status_before["candidates"][0]["state"] == "running"

    monkeypatch.setattr(fanout_runner, "_run_subagent", _fake_worker(_good_change))
    # `resume` (service.py) is just `run_all` again: it only ever touches
    # non-terminal candidates, so calling it a second time IS the resume.
    asyncio.run(fanout_runner.run_all(run_id, OWNER))

    status_after = fanout_service.status(run_id, OWNER)
    assert status_after["status"] == "done"
    assert status_after["candidates"][0]["state"] == "done"


# ---------------------------------------------------------------------------
# Apply: two steps (propose, then confirm)
# ---------------------------------------------------------------------------
def test_apply_is_two_steps(repo, monkeypatch):
    monkeypatch.setattr(fanout_runner, "_run_subagent", _fake_worker(_good_change))
    plan = _plan(repo, labels=("only",))
    run_id = fanout_runner.start(OWNER, plan)
    asyncio.run(fanout_runner.run_all(run_id, OWNER))

    preview = fanout_service.apply(run_id, OWNER, "only", confirm=False)
    assert preview["proposed"] is True
    assert preview["applied"] is False
    with open(repo / "feature.py", encoding="utf-8") as fh:
        assert fh.read() == "VALUE = False\n"  # nothing written yet

    confirmed = fanout_service.apply(run_id, OWNER, "only", confirm=True)
    assert confirmed["applied"] is True
    with open(repo / "feature.py", encoding="utf-8") as fh:
        assert fh.read() == "VALUE = True\n"


# ---------------------------------------------------------------------------
# Budget: reserved before launch, reconciled after
# ---------------------------------------------------------------------------
def test_budget_reserved_and_reconciled(repo, monkeypatch):
    monkeypatch.setattr(fanout_runner, "_run_subagent", _fake_worker(_good_change))
    plan = _plan(repo, labels=("only",))
    plan.budget_tokens = 1_000_000
    run_id = fanout_runner.start(OWNER, plan)
    asyncio.run(fanout_runner.run_all(run_id, OWNER))

    snap = budget_account.snapshot(run_id)
    assert snap["opened"] is True
    assert snap["reserved_tokens"] == 0  # the one reservation was reconciled
    assert snap["consumed_tokens"] == 111 + 47  # from `_fake_worker`'s counts... see below


def test_budget_not_exceeded_refuses_the_candidate(repo, monkeypatch):
    monkeypatch.setattr(fanout_runner, "_run_subagent", _fake_worker(_good_change))
    plan = _plan(repo, labels=("only",))
    plan.max_rounds = 100  # forces a huge reservation ask
    plan.budget_tokens = 10  # far below what one round would need
    run_id = fanout_runner.start(OWNER, plan)
    manifest = asyncio.run(fanout_runner.run_all(run_id, OWNER))
    cand = fanout_runner.load_candidate(run_id, "only")
    assert cand["state"] == "error"
    assert "budget" in (cand["error"] or "").lower() or "exceeded" in (cand["error"] or "").lower()


# ---------------------------------------------------------------------------
# Concurrency: bounded by the setting
# ---------------------------------------------------------------------------
def test_concurrency_bounded_by_setting(repo, monkeypatch):
    overrides = {"agent_fanout_max_parallel": 1}
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: overrides.get(key, default))

    concurrent = {"now": 0, "max": 0}

    async def fake(run, *, workspace, **kwargs):
        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        await asyncio.sleep(0.05)
        _good_change(run, workspace)
        run.stop_reason = "complete"
        import time
        run.finished = time.time()
        concurrent["now"] -= 1
    monkeypatch.setattr(fanout_runner, "_run_subagent", fake)

    # Three REMOTE-looking candidates so the extra "local" lane (always 1)
    # does not confound this assertion.
    plan = FanoutPlan(
        prompt="race", workspace=str(repo),
        candidates=[
            FanoutCandidate(label=f"c{i}", model="m", endpoint_url=f"https://remote{i}.example/v1")
            for i in range(3)
        ],
        max_rounds=2, budget_tokens=0, project_id="proj1",
    )
    run_id = fanout_runner.start(OWNER, plan)
    asyncio.run(fanout_runner.run_all(run_id, OWNER))
    assert concurrent["max"] <= 1


# ---------------------------------------------------------------------------
# score.py: unit-level checks independent of any worker/repo
# ---------------------------------------------------------------------------
def test_score_unknown_cost_is_not_treated_as_free():
    cands = [
        {"label": "priced", "state": "done", "tests": {"ok": True}, "diff": {"additions": 5, "deletions": 0},
         "cost": 1.0, "latency_s": 2.0, "worker_report": {"status": "done", "rejections": 0, "failed_calls": 0}},
        {"label": "unpriced", "state": "done", "tests": {"ok": True}, "diff": {"additions": 5, "deletions": 0},
         "cost": None, "latency_s": 2.0, "worker_report": {"status": "done", "rejections": 0, "failed_calls": 0}},
    ]
    ranking = fanout_score.rank(cands)
    by_label = {r["label"]: r for r in ranking}
    # Identical on every other dimension: the unpriced one must NOT win on
    # cost alone (it would if unknown scored as free==1.0).
    assert by_label["unpriced"]["dimensions"]["cost"] < 1.0
    assert by_label["priced"]["dimensions"]["cost"] > by_label["unpriced"]["dimensions"]["cost"]


def test_score_huge_diff_is_penalised():
    small = {"label": "small", "state": "done", "tests": {"ok": True},
             "diff": {"additions": 10, "deletions": 0}, "cost": None, "latency_s": 1.0,
             "worker_report": {"status": "done"}}
    huge = {"label": "huge", "state": "done", "tests": {"ok": True},
            "diff": {"additions": 5000, "deletions": 0}, "cost": None, "latency_s": 1.0,
            "worker_report": {"status": "done"}}
    ranking = fanout_score.rank([small, huge])
    by_label = {r["label"]: r for r in ranking}
    assert by_label["small"]["dimensions"]["diff_size"] > by_label["huge"]["dimensions"]["diff_size"]


# ---------------------------------------------------------------------------
# Diff between two candidates
# ---------------------------------------------------------------------------
def test_diff_between_candidates(repo, monkeypatch):
    async def fake(run, *, workspace, **kwargs):
        if run.name == "good":
            _good_change(run, workspace)
        else:
            _bad_change(run, workspace)
        run.stop_reason = "complete"
        import time
        run.finished = time.time()
    monkeypatch.setattr(fanout_runner, "_run_subagent", fake)

    plan = _plan(repo)
    run_id = fanout_runner.start(OWNER, plan)
    asyncio.run(fanout_runner.run_all(run_id, OWNER))

    diff = fanout_service.diff_between(run_id, OWNER, "good", "bad")
    assert "feature.py" in diff["files"]
    assert "True" in diff["files"]["feature.py"]
    assert "False" in diff["files"]["feature.py"]


# ---------------------------------------------------------------------------
# Tool + route registration
# ---------------------------------------------------------------------------
def test_tools_are_registered():
    import src.agent_tools as agent_tools
    import src.tool_schemas as tool_schemas
    import src.tool_capabilities as tool_capabilities

    for name in ("fanout_run", "fanout_status", "fanout_results", "fanout_apply"):
        assert name in agent_tools.TOOL_HANDLERS
        assert name in agent_tools.TOOL_TAGS
        assert name in tool_capabilities.TOOL_CAPABILITIES
    schema_names = {f["function"]["name"] for f in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert {"fanout_run", "fanout_status", "fanout_results", "fanout_apply"} <= schema_names


def test_routes_are_registered():
    from routes.fanout_routes import setup_fanout_routes
    router = setup_fanout_routes()
    paths = {route.path for route in router.routes}
    assert "/api/fanout" in paths
    assert "/api/fanout/{run_id}" in paths
    assert "/api/fanout/{run_id}/results" in paths
    assert "/api/fanout/{run_id}/apply" in paths


def test_default_candidates_never_fewer_than_two(monkeypatch):
    overrides = {"agent_subagent_worker_model": "", "dispatch_model": "", "agent_fanout_model_pool": ""}
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: overrides.get(key, default))
    from src.fanout.plan import from_settings_default_candidates
    candidates = from_settings_default_candidates(coordinator_model="my-model", coordinator_endpoint_url="http://x")
    assert len(candidates) >= 2
