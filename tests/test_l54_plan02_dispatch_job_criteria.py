"""Lote 54 / PLAN-02: job-level acceptance criteria in `src/dispatch.py`.

`src/agent_tools/subagent_tools.py::verify_criteria` already checks a single
CHILD's own criteria against its own evidence (Lote 46) and `SubagentRun`
downgrades that one worker's status accordingly. What was still missing —
named explicitly by `docs/spec/v2/P1_COMUN.md`'s Lote 54 assignment ("el
contrato de delegación con criterios verificados también en dispatch") — is
the JOB's own criteria: a job can finish with every worker reporting `done`
and the verification passing while still not having done what the
COORDINATOR asked of the job as a whole (two workers each touch their own
file, neither touches the one the job's criterion names). `_settle` now
checks `job.args["criteria"]` against `job.changes` — Faustus's own observed
diff, never a worker's claim — reusing `verify_criteria` as-is (rule 4: no
second checker).
"""
from __future__ import annotations

import pytest

from src import dispatch


@pytest.fixture(autouse=True)
def _clean():
    dispatch.reset_for_tests()
    yield
    dispatch.reset_for_tests()


def _job(criteria=None, tasks=None):
    args = {"tasks": tasks or [{"name": "w1", "instruction": "a"}], "parallel": True, "timeout_s": 60}
    if criteria is not None:
        args["criteria"] = criteria
    job = dispatch.DispatchJob("luis", args, None, "", "m", None, "t")
    job.status = "running"
    dispatch._jobs[job.id] = job
    return job


def _done_subagent(name="w1", mutations=(), tool_calls=1, status="done", error=None):
    return {"name": name, "status": status, "mutations": list(mutations),
            "tool_calls": tool_calls, "error": error}


# ── build_args: a job-level criteria field is parsed and normalized ────────

def test_build_args_carries_a_job_level_criteria_field():
    args = dispatch.build_args({
        "tasks": ["do the thing"],
        "criteria": ["config.py is updated with the new default"],
    })
    assert args["criteria"] == ["config.py is updated with the new default"]


def test_build_args_without_criteria_is_unchanged():
    """The additive case: a request with no job-level criteria produces
    exactly the args dict it always has — no `criteria` key at all."""
    args = dispatch.build_args({"tasks": ["do the thing"]})
    assert "criteria" not in args


def test_build_args_accepts_a_bare_string_criteria():
    args = dispatch.build_args({"tasks": ["do the thing"], "criteria": "tests pass"})
    assert args["criteria"] == ["tests pass"]


# ── _settle: the criterion is checked against the job's OWN observed diff ──

def test_unmet_job_criteria_downgrades_an_otherwise_done_job():
    job = _job(criteria=["config.py is updated with the new default"])
    job.result = {"subagents": [_done_subagent(mutations=["other.py"])]}
    job.changes = {"added": [], "modified": ["other.py"], "deleted": []}
    job.verification = None
    dispatch._settle(job)
    assert job.status == "partial"
    assert job.criteria_check is not None and job.criteria_check["complete"] is False
    assert "config.py" in job.criteria_check["unmet"][0]
    assert "job criteria unmet" in job.verdict


def test_met_job_criteria_leaves_a_clean_job_done():
    job = _job(criteria=["config.py is updated with the new default"])
    job.result = {"subagents": [_done_subagent(mutations=["config.py"])]}
    job.changes = {"added": [], "modified": ["config.py"], "deleted": []}
    job.verification = None
    dispatch._settle(job)
    assert job.status == "done"
    assert job.criteria_check is not None and job.criteria_check["complete"] is True
    assert "job criteria unmet" not in job.verdict


def test_a_job_with_no_declared_criteria_is_unaffected():
    """The invariant this whole feature has to preserve: a job that declares
    no job-level criteria settles exactly as it did before this existed."""
    job = _job(criteria=None)
    job.result = {"subagents": [_done_subagent(mutations=["x.py"])]}
    job.changes = {"added": [], "modified": ["x.py"], "deleted": []}
    job.verification = None
    dispatch._settle(job)
    assert job.status == "done"
    assert job.criteria_check is None
    assert "criteria" not in job.to_dict()


def test_the_check_looks_at_the_job_observed_diff_not_a_worker_claim():
    """A worker CLAIMING it touched config.py, without Faustus's own diff
    backing it (job.changes), must not satisfy the criterion — the exact
    "never a worker's claim" property `verify_criteria` already guarantees at
    the per-child level, now asserted at the job level too."""
    job = _job(criteria=["config.py is updated with the new default"])
    job.result = {"subagents": [_done_subagent(mutations=["config.py"])]}
    job.changes = {"added": [], "modified": ["other.py"], "deleted": []}  # disk disagrees
    job.verification = None
    dispatch._settle(job)
    assert job.status == "partial"
    assert job.criteria_check["complete"] is False


def test_criteria_check_is_visible_on_to_dict():
    job = _job(criteria=["tests pass"])
    job.result = {"subagents": [_done_subagent(mutations=[], tool_calls=0)]}
    job.changes = {"added": [], "modified": [], "deleted": []}
    job.verification = None
    dispatch._settle(job)
    d = job.to_dict()
    assert d["criteria"] == ["tests pass"]
    assert d["criteria_check"]["complete"] is False
    # brief (include_result=False) still shows the DECLARATION, just not the
    # evidence-derived check — same asymmetry `expected_output_contains`
    # already has between the two views.
    brief = job.to_dict(include_result=False)
    assert brief["criteria"] == ["tests pass"]
    assert "criteria_check" not in brief


def test_an_already_partial_or_errored_job_is_not_relabelled():
    """The criteria gate only ever downgrades `done` — it must never paper
    over (or re-flag past) a status the worker/verification machinery already
    decided on its own terms."""
    job = _job(criteria=["config.py is updated"])
    job.result = {"subagents": [_done_subagent(mutations=["x.py"], status="error", error="boom")]}
    job.changes = {"added": [], "modified": ["x.py"], "deleted": []}
    job.verification = None
    dispatch._settle(job)
    assert job.status == "partial"  # from the bad worker status, not (only) criteria
    assert job.criteria_check["complete"] is False  # still computed and reported


def test_a_criteria_check_failure_never_raises():
    """`_build_job_criteria_check` swallows its own exceptions (the settle
    path must never fail over this) — simulate one by corrupting `changes`."""
    job = _job(criteria=["config.py is updated"])
    job.result = {"subagents": [_done_subagent(mutations=["config.py"])]}
    job.changes = None  # no observed diff at all (e.g. checkpoint unavailable)
    job.verification = None
    dispatch._settle(job)  # must not raise
    assert job.status in ("partial", "done")
