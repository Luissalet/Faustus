"""The output oracle's declaration has to be PRIOR, or it proves nothing.

`src/output_oracle.py` unmasks a verification that exits 0 without doing the
job — a pytest collection that found nothing says "no tests ran" and exits 0,
and a custom `verify` command that succeeded at doing nothing does the same.
`project_tests.run_tests` already consults it. What was missing is the half
that gives it any evidential weight: the expectation has to be written down
when the plan is written, before anything runs. A string chosen after seeing
the output is a description of what happened, not a test of it.

So this file follows one declaration the whole way:

    POST /api/dispatch {expected_output_contains: "47 passed"}
        → validated at the door, while the caller still holds it
        → stored on the job
        → onto the mirror, so a crash cannot quietly drop it
        → back off the mirror, and into the recovery plan
        → into the spec that reaches `project_tests.run_tests`

and the invariant under all of it: a job that declares nothing is byte for
byte the job it was before any of this existed.
"""

from __future__ import annotations

import json

import pytest

from src import crash_recovery, dispatch
from src.output_oracle import EXIT_OUTPUT_MISMATCH, MAX_EXPECTED_CHARS


# ── the door ───────────────────────────────────────────────────────────────

def test_a_declaration_is_read_off_the_request():
    *_, expected = dispatch._verify_options({"expected_output_contains": "47 passed"})
    assert expected == "47 passed"


def test_declaring_nothing_is_the_normal_case():
    for body in ({}, {"expected_output_contains": None}):
        assert dispatch._verify_options(body)[4] is None


@pytest.mark.parametrize("bad,why", [
    (123, "must be a string"),
    ("   ", "must not be blank"),
    # An empty string is a declaration that cannot fail, which is worse than
    # none: omit the key to mean "do not check".
    ("", "must not be blank"),
    ("x" * (MAX_EXPECTED_CHARS + 1), "at most"),
])
def test_a_declaration_that_cannot_be_checked_is_refused_at_the_door(bad, why):
    """Refused while the caller is still holding it — not silently ignored
    later by a runner it has already reached."""
    with pytest.raises(ValueError) as e:
        dispatch._verify_options({"expected_output_contains": bad})
    assert "expected_output_contains" in str(e.value) and why in str(e.value)


def test_the_other_verify_options_are_unchanged_by_the_new_one():
    verify, scope, fix, timeout, expected = dispatch._verify_options(
        {"verify": "pytest -q", "verify_scope": "all", "fix_rounds": 2,
         "verify_timeout_s": 120, "expected_output_contains": "47 passed"})
    assert (verify, scope, fix, timeout) == ("pytest -q", "all", 2, 120.0)
    assert expected == "47 passed"


# ── the mirror, and back ───────────────────────────────────────────────────

def _job(expected):
    return dispatch.DispatchJob(
        "luis", {"tasks": [{"name": "w1", "instruction": "add apply_tax"}]},
        "/ws", "http://x/v1", "m", None, "Workers", verify="pytest -q",
        expected_output=expected)


def test_the_declaration_survives_the_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path))
    dispatch.reset_for_tests()
    job = _job("47 passed")
    job._persist()

    again = dispatch.get(job.id)
    assert again is not None and again.expected_output == "47 passed"

    # …and the recovery plan re-pins it beside the model and the parameters.
    entry = crash_recovery._read_dispatch_mirror(str(tmp_path / f"{job.id}.json"))
    assert entry["params"]["expected_output_contains"] == "47 passed"


def test_a_job_that_declares_nothing_puts_no_key_on_the_wire(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path))
    dispatch.reset_for_tests()
    job = _job(None)
    assert "expected_output_contains" not in json.dumps(job.to_dict())

    job._persist()
    again = dispatch.get(job.id)
    assert again is not None and again.expected_output is None
    entry = crash_recovery._read_dispatch_mirror(str(tmp_path / f"{job.id}.json"))
    assert entry["params"]["expected_output_contains"] is None


# ── into the spec that actually runs ───────────────────────────────────────

@pytest.fixture
def ran(tmp_path, monkeypatch):
    """`run_verification` with a detected runner and a captured `run_tests`."""
    from src import project_tests as pt

    seen = {}

    def fake_detect(workspace, override=None):
        return {"kind": "pytest", "argv": ["python", "-m", "pytest"], "command": "pytest -q"}

    def fake_run_tests(workspace, spec, **kwargs):
        seen["spec"] = dict(spec)
        return {"ran": True, "ok": True, "kind": "pytest", "command": "pytest -q",
                "exit_code": 0, "summary": "47 passed", "failures": [], "output_tail": "47 passed",
                "output_matched": None}

    monkeypatch.setattr(pt, "detect_test_command", fake_detect)
    monkeypatch.setattr(pt, "run_tests", fake_run_tests)
    seen["dir"] = str(tmp_path)
    return seen


def test_the_declaration_reaches_the_spec_run_tests_reads(ran):
    dispatch.run_verification(ran["dir"], "pytest -q", [], expected_output="47 passed")
    assert ran["spec"]["expected_output_contains"] == "47 passed"


def test_declaring_nothing_leaves_the_spec_unchecked(ran):
    dispatch.run_verification(ran["dir"], "pytest -q", [])
    # The key may be present, but None is what `output_oracle.check` reads as
    # "nothing was declared, so nothing was checked".
    assert ran["spec"].get("expected_output_contains") is None


def test_the_verdict_says_why_it_was_overturned(tmp_path, monkeypatch):
    """A `ran: True, ok: False` beside a summary saying "47 passed" is a
    verdict no reader can explain."""
    from src import project_tests as pt

    monkeypatch.setattr(pt, "detect_test_command",
                        lambda ws, override=None: {"kind": "pytest", "argv": ["x"], "command": "pytest -q"})
    monkeypatch.setattr(pt, "run_tests", lambda ws, spec, **kw: {
        "ran": True, "ok": False, "kind": "pytest", "command": "pytest -q",
        "exit_code": EXIT_OUTPUT_MISMATCH, "summary": "no tests ran",
        "failures": [], "output_tail": "no tests ran in 0.01s", "output_matched": False})

    out = dispatch.run_verification(str(tmp_path), "pytest -q", [], expected_output="47 passed")
    assert out["ok"] is False and out["exit_code"] == EXIT_OUTPUT_MISMATCH
    assert out["output_matched"] is False
    assert out["expected_output_contains"] == "47 passed"


def test_an_undeclared_run_carries_neither_key(ran):
    out = dispatch.run_verification(ran["dir"], "pytest -q", [])
    assert "output_matched" not in out and "expected_output_contains" not in out


def test_a_run_with_no_runner_never_touches_the_spec(tmp_path, monkeypatch):
    """The declaration is attached AFTER the "is there anything to run"
    guard, so a workspace with no test runner answers exactly as before."""
    from src import project_tests as pt

    monkeypatch.setattr(pt, "detect_test_command", lambda ws, override=None: None)
    out = dispatch.run_verification(str(tmp_path), "auto", [], expected_output="47 passed")
    assert out == {"mode": "auto", "ran": False, "ok": None,
                   "summary": "no test runner detected in the workspace "
                              "(give `verify` a command that proves the task)"}
