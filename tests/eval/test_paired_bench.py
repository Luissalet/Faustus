"""H24 - the paired harness bench against a real server process.

Each test boots its own app (own data dir, own port, own scripted model
process), so they are slow (tens of seconds each) and marked ``slow``. They
prove the bench measures real outcomes and boundaries, that a setting arm
really reaches the server, and that the recovery scenarios hold.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from src.bench import harness_pair as hp
from tests.eval import paired_cases as pc
from tests.eval import tasks as T
from tests.eval.harness import EvalApp

pytestmark = [pytest.mark.slow, pytest.mark.timeout(400)]


def test_a_task_case_is_scored_on_its_outcome_with_clean_boundaries():
    rec = pc.run_case("bug_fix", arm="candidate", repeat=0)
    assert rec.success and rec.finished and not rec.error
    assert rec.violations == []
    assert rec.rounds >= 1 and rec.tool_calls >= 2 and rec.seconds > 0
    assert rec.tokens is not None  # the scripted model reports usage
    assert set(rec.tools_used) >= {"read_file", "edit_file"}


def test_a_change_outside_the_allowed_files_is_reported_as_a_violation():
    strict = pc.PairedCase("bug_fix_strict", T.BUG_FIX, allowed_changes=("nothing.txt",))
    sb = hp.Sandbox.create()
    app = pc.make_app(sb, repo=None, settings=None, endpoint=None, model=None)
    try:
        app.start()
        rec = pc.run_task_case(strict, sb, app, arm="candidate", repeat=0)
    finally:
        app.stop()
        sb.cleanup()
    assert rec.success  # the task itself worked
    assert [v["kind"] for v in rec.violations] == [hp.V_UNEXPECTED_FILE]
    assert "calc.py" in rec.violations[0]["detail"]


def test_a_failed_outcome_is_not_reported_as_success():
    wrong = T.Task(name="bug_fix", category="bug_fix", message=T.BUG_FIX.message,
                   script=["I looked and everything is fine."],  # never edits the file
                   setup=T.BUG_FIX.setup, verify=T.BUG_FIX.verify)
    case = pc.PairedCase("bug_fix_unfixed", wrong, ("calc.py",))
    sb = hp.Sandbox.create()
    app = pc.make_app(sb, repo=None, settings=None, endpoint=None, model=None)
    try:
        app.start()
        rec = pc.run_task_case(case, sb, app, arm="candidate", repeat=0)
    finally:
        app.stop()
        sb.cleanup()
    assert rec.finished and not rec.success
    assert rec.violations == []


def test_a_setting_arm_reaches_the_server_and_the_default_arm_does_not_carry_it():
    sb_legacy, sb_default = hp.Sandbox.create(), hp.Sandbox.create()
    legacy = pc.make_app(sb_legacy, repo=None, settings={"llm_projection_mode": "legacy"}, endpoint=None, model=None)
    default = pc.make_app(sb_default, repo=None, settings=None, endpoint=None, model=None)
    try:
        legacy.start()
        default.start()

        def mode(app: EvalApp) -> str:
            with urllib.request.urlopen(app.base + "/api/auth/settings", timeout=15) as r:
                return json.loads(r.read().decode("utf-8")).get("llm_projection_mode", "<unset>")

        assert mode(legacy) == "legacy"
        assert mode(default) in ("canonical", "<unset>")
        assert legacy.data_dir != default.data_dir and legacy.port != default.port
    finally:
        legacy.stop()
        default.stop()
        sb_legacy.cleanup()
        sb_default.cleanup()


def test_runs_share_nothing_and_leave_nothing(monkeypatch):
    created = []
    real_create = hp.Sandbox.create.__func__

    def recording_create(cls, *a, **kw):
        sb = real_create(cls, *a, **kw)
        created.append(sb)
        return sb

    monkeypatch.setattr(hp.Sandbox, "create", classmethod(recording_create))
    first = pc.run_case("document", arm="a", repeat=0)
    second = pc.run_case("document", arm="b", repeat=0)
    assert first.success and second.success
    assert first.violations == [] and second.violations == []
    assert len(created) == 2 and created[0].root != created[1].root
    assert created[0].data != created[1].data
    assert not any(sb.root.exists() for sb in created), "a run left its sandbox behind"


def test_killing_the_server_mid_command_neither_repeats_nor_forgets_it():
    rec = pc.run_case("recovery_kill_mid_tool", arm="candidate", repeat=0)
    assert rec.error is None, rec.error
    assert rec.recovery["effect_count_at_kill"] == 1
    assert rec.recovery["effect_count_after_restart"] == 1, "the command must not run again after a restart"
    assert any(s == "interrupted" for s in rec.recovery["run_logs"].values()) or rec.recovery["session_marked"]["marked"]
    assert rec.success, rec.detail
    assert rec.violations == []


def test_an_approval_answered_after_a_restart_runs_once_or_is_refused_out_loud():
    rec = pc.run_case("recovery_approval_after_restart", arm="candidate", repeat=0)
    assert rec.error is None, rec.error
    assert rec.recovery["approval_offered"] is True
    assert rec.recovery["effect_count_before"] == 0, "nothing may run before the approval"
    assert rec.recovery["outcome"] in ("executed_once", "refused_explicitly"), rec.recovery
    assert rec.success, rec.detail
    assert rec.violations == []


def test_a_bench_of_a_bad_case_name_fails_loudly():
    with pytest.raises(KeyError):
        pc.run_case("no_such_case", arm="a", repeat=0)


def test_a_run_that_cannot_reach_its_model_is_a_failed_run_not_a_crash():
    rec = pc.run_case("document", arm="a", repeat=0, endpoint="http://127.0.0.1:9/v1", model="none", timeout=20)
    assert not rec.success
    assert rec.violations == []
