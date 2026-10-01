"""H24 - the pure half of the paired harness bench: sandboxes, boundary and
isolation checks, aggregation, the before/after comparison, and the CLI's arm
handling. No server, no model."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.bench import harness_pair as hp

REPO = Path(__file__).resolve().parents[1]


def _rec(arm="a", case="c", repeat=0, success=True, seconds=10.0, rounds=3, tokens=(100, 50),
         violations=(), finished=True):
    return hp.RunRecord(arm=arm, case=case, repeat=repeat, success=success, finished=finished, rounds=rounds,
                        seconds=seconds, tokens_in=tokens[0], tokens_out=tokens[1],
                        violations=[{"kind": k, "detail": "x"} for k in violations])


def _runs(arm, case="c", n=3, **kw):
    return [_rec(arm=arm, case=case, repeat=i, **kw) for i in range(n)]


# -- sandbox and boundaries ----------------------------------------------------

def test_sandbox_has_workspace_canary_and_data_and_cleans_up():
    sb = hp.Sandbox.create()
    try:
        assert sb.workspace.is_dir() and sb.canary.is_dir() and sb.data.is_dir()
        assert (sb.canary / "secret.txt").is_file()
        assert len({sb.workspace, sb.canary, sb.data}) == 3
    finally:
        sb.cleanup()
    assert not sb.leftover()


def test_two_sandboxes_never_share_a_root():
    a, b = hp.Sandbox.create(), hp.Sandbox.create()
    try:
        assert a.root != b.root
        (a.workspace / "x.txt").write_text("a")
        assert not (b.workspace / "x.txt").exists()
    finally:
        a.cleanup(), b.cleanup()


def test_allowed_change_is_not_a_violation_and_unexpected_one_is():
    sb = hp.Sandbox.create()
    try:
        (sb.workspace / "calc.py").write_text("v1")
        sb.snapshot()
        (sb.workspace / "calc.py").write_text("v2")
        assert hp.check_boundaries(sb, allowed_changes=("calc.py",)) == []
        (sb.workspace / "other.txt").write_text("x")
        kinds = [v.kind for v in hp.check_boundaries(sb, allowed_changes=("calc.py",))]
        assert kinds == [hp.V_UNEXPECTED_FILE]
    finally:
        sb.cleanup()


def test_deleting_a_file_the_task_did_not_allow_is_a_violation():
    sb = hp.Sandbox.create()
    try:
        (sb.workspace / "keep.txt").write_text("k")
        sb.snapshot()
        (sb.workspace / "keep.txt").unlink()
        out = hp.check_boundaries(sb, allowed_changes=("calc.py",))
        assert [v.kind for v in out] == [hp.V_UNEXPECTED_FILE]
        assert "keep.txt" in out[0].detail
    finally:
        sb.cleanup()


def test_canary_change_is_a_violation_even_when_the_workspace_is_clean():
    sb = hp.Sandbox.create()
    try:
        sb.snapshot()
        (sb.canary / "secret.txt").write_text("tampered")
        out = hp.check_boundaries(sb)
        assert [v.kind for v in out] == [hp.V_CANARY]
        (sb.canary / "secret.txt").unlink()
        assert hp.V_CANARY in [v.kind for v in hp.check_boundaries(sb)]
    finally:
        sb.cleanup()


def test_bookkeeping_files_are_not_task_changes():
    sb = hp.Sandbox.create()
    try:
        sb.snapshot()
        (sb.workspace / "__pycache__").mkdir()
        (sb.workspace / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"x")
        (sb.workspace / ".pytest_cache").mkdir()
        (sb.workspace / ".pytest_cache" / "v").write_text("x")
        (sb.workspace / ".faustus_edit_history").mkdir()
        (sb.workspace / ".faustus_edit_history" / "h.jsonl").write_text("x")
        assert hp.check_boundaries(sb) == []
    finally:
        sb.cleanup()


def test_forbidden_tool_and_duplicate_effect_are_violations():
    sb = hp.Sandbox.create()
    try:
        sb.snapshot()
        out = hp.check_boundaries(sb, tools_used=["read_file", "web_search", "web_search"],
                                  forbidden_tools=("web_search",), effect_counts={"mail": (2, 1), "file": (1, 1)})
        assert sorted(v.kind for v in out) == sorted([hp.V_FORBIDDEN_TOOL, hp.V_DUPLICATE_EFFECT])
    finally:
        sb.cleanup()


# -- isolation ------------------------------------------------------------------

def test_no_leak_when_nothing_changed(tmp_path):
    before = hp.capture_process_state([tmp_path])
    assert hp.state_leaks(before, hp.capture_process_state([tmp_path])) == []


def test_changed_cwd_is_a_leak(tmp_path):
    before = hp.capture_process_state()
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        out = hp.state_leaks(before, hp.capture_process_state())
    finally:
        os.chdir(old)
    assert [v.kind for v in out] == [hp.V_LEAK] and "working directory" in out[0].detail


def test_changed_watched_env_var_is_a_leak(monkeypatch):
    before = hp.capture_process_state()
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", "/somewhere/else")
    out = hp.state_leaks(before, hp.capture_process_state())
    assert any("ODYSSEUS_DATA_DIR" in v.detail for v in out)


def test_stand_in_module_left_behind_is_a_leak(monkeypatch):
    from unittest import mock
    before = hp.capture_process_state()
    monkeypatch.setitem(sys.modules, "src._fake_leak_probe", mock.MagicMock())
    out = hp.state_leaks(before, hp.capture_process_state())
    assert any("src._fake_leak_probe" in v.detail for v in out)


def test_files_added_to_a_watched_directory_are_a_leak(tmp_path):
    (tmp_path / "existing.txt").write_text("a")
    before = hp.capture_process_state([tmp_path])
    (tmp_path / "new.txt").write_text("b")
    out = hp.state_leaks(before, hp.capture_process_state([tmp_path]))
    assert out and "1 added" in out[0].detail


def test_files_changed_or_removed_in_a_watched_directory_are_a_leak(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    before = hp.capture_process_state([tmp_path])
    (tmp_path / "a.txt").write_text("a longer text")
    (tmp_path / "b.txt").unlink()
    out = hp.state_leaks(before, hp.capture_process_state([tmp_path]))
    assert out and "1 removed" in out[0].detail and "1 changed" in out[0].detail


def test_guard_fails_a_test_that_leaks_and_passes_one_that_does_not(tmp_path):
    from tests.eval import isolation
    clean = isolation.guard()
    next(clean)
    with pytest.raises(StopIteration):
        next(clean)

    dirty = isolation.guard(extra_dirs=[tmp_path])
    next(dirty)
    (tmp_path / "leftover.txt").write_text("leak")
    with pytest.raises(pytest.fail.Exception) as info:
        next(dirty)
    assert "leaked global state" in str(info.value)


def test_data_dir_holding_another_cases_files_is_flagged():
    sb = hp.Sandbox.create()
    try:
        (sb.data / "session-abc123.json").write_text("{}")
        assert hp.stale_state_in(sb, ["zzz"]) == []
        out = hp.stale_state_in(sb, ["abc123"])
        assert [v.kind for v in out] == [hp.V_STALE_CASE_STATE]
        assert hp.stale_state_in(sb, []) == []
    finally:
        sb.cleanup()


# -- numbers --------------------------------------------------------------------

def test_tokens_unknown_stays_unknown():
    assert hp.tokens_of(None) == (None, None)
    assert hp.tokens_of({}) == (None, None)
    assert hp.tokens_of({"input_tokens": 12, "output_tokens": 3}) == (12, 3)
    assert hp.tokens_of({"input_tokens": True, "output_tokens": "x"}) == (None, None)
    assert _rec(tokens=(None, None)).tokens is None
    assert _rec(tokens=(5, None)).tokens == 5


def test_percentile_and_summarize():
    assert hp.percentile([], 0.95) is None
    assert hp.percentile([5], 0.95) == 5
    assert hp.percentile([1, 2, 3, 4, 100], 0.95) == 100
    s = hp.summarize([_rec(seconds=2), _rec(seconds=4, success=False, tokens=(None, None), violations=["canary_changed"])])
    assert s["runs"] == 2 and s["success"] == 1 and s["success_rate"] == 0.5
    assert s["seconds_median"] == 3 and s["tokens_unknown"] == 1 and s["violations"] == 1
    assert s["violation_kinds"] == ["canary_changed"]
    assert hp.summarize([])["success_rate"] is None


# -- comparison -----------------------------------------------------------------

def test_equal_arms_are_equivalent():
    out = hp.compare(_runs("b"), _runs("c"))
    assert out["verdict"] == "equivalent"
    assert out["comparison_kind"] == "paired (same model)"


def test_a_success_drop_is_a_regression_whatever_the_speed():
    out = hp.compare(_runs("b", seconds=10), _runs("c", seconds=1, success=False))
    assert out["verdict"] == "regression" and out["cases"]["c"]["verdict"] == "regression"


def test_a_new_boundary_violation_blocks_even_when_faster_and_successful():
    out = hp.compare(_runs("b", seconds=10), _runs("c", seconds=2, violations=["canary_changed"]))
    assert out["verdict"] == "blocked"
    assert "c" in out["reason"]


def test_faster_and_slower_need_the_band_to_be_crossed():
    assert hp.compare(_runs("b", seconds=10), _runs("c", seconds=5))["verdict"] == "faster"
    assert hp.compare(_runs("b", seconds=10), _runs("c", seconds=20))["verdict"] == "slower"
    assert hp.compare(_runs("b", seconds=10), _runs("c", seconds=9.5))["verdict"] == "equivalent"


def test_too_few_repeats_is_inconclusive_not_a_percentage():
    out = hp.compare(_runs("b", n=2), _runs("c", n=2, seconds=1), min_repeats=3)
    assert out["verdict"] == "inconclusive" and "fewer than 3" in out["reason"]


def test_a_case_on_one_arm_only_is_unpaired_and_does_not_count():
    base = _runs("b") + _runs("b", case="only-base")
    out = hp.compare(base, _runs("c"))
    assert out["cases"]["only-base"]["verdict"] == "unpaired"
    assert out["verdict"] == "equivalent"
    assert hp.compare([], [])["verdict"] == "inconclusive"


def test_regression_in_one_case_is_not_hidden_by_improvement_in_another():
    base = _runs("b", case="x", seconds=10) + _runs("b", case="y", seconds=10)
    cand = _runs("c", case="x", seconds=1) + _runs("c", case="y", seconds=10, success=False)
    out = hp.compare(base, cand)
    assert out["verdict"] == "regression" and "y" in out["reason"]


def test_models_that_differ_are_labelled_whole_system():
    out = hp.compare(_runs("b"), _runs("c"), same_model=False)
    assert out["comparison_kind"] == "whole-system (models differ)"


# -- reports --------------------------------------------------------------------

def test_report_round_trip_and_listing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    assert hp.list_reports() == []
    report = hp.build_report({"mode": "scripted model", "model": "m", "endpoint": "e", "effort": "x", "same_model": True},
                             {"baseline": _runs("baseline"), "candidate": _runs("candidate")},
                             baseline="baseline", candidate="candidate")
    d = Path(hp.reports_dir())
    d.mkdir(parents=True)
    (d / "20260101-000000.json").write_text(json.dumps(report))
    (d / "broken.json").write_text("{not json")
    (d / "other.json").write_text(json.dumps({"schema_version": 99}))
    rows = hp.list_reports()
    assert [r["file"] for r in rows] == ["20260101-000000.json"]
    assert rows[0]["verdict"] == "equivalent"
    text = hp.render_text(hp.load_report(str(d / "20260101-000000.json")))
    assert "equivalent" in text and "baseline: 3/3 ok" in text
    with pytest.raises(ValueError):
        hp.load_report(str(d / "other.json"))


# -- the CLI ---------------------------------------------------------------------

def _cli():
    spec = importlib.util.spec_from_file_location("harness_paired_bench", REPO / "scripts" / "harness_paired_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_parses_settings_with_typed_values():
    cli = _cli()
    assert cli.parse_settings("a=1,b=true,c=text,d=2.5") == {"a": 1, "b": True, "c": "text", "d": 2.5}
    with pytest.raises(SystemExit):
        cli.parse_settings("novalue")


def test_cli_arms_current_and_settings(tmp_path):
    cli = _cli()
    cur = cli.make_arm("candidate", "current", tmp_path)
    assert cur.repo == cli.REPO and cur.settings == {}
    st = cli.make_arm("baseline", "set:llm_projection_mode=legacy", tmp_path)
    assert st.repo == cli.REPO and st.settings == {"llm_projection_mode": "legacy"}
    with pytest.raises(SystemExit):
        cli.make_arm("x", "banana", tmp_path)


def test_cli_revision_arm_uses_a_detached_worktree_and_removes_it(tmp_path, monkeypatch):
    cli = _cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                                    cwd=str(repo), check=True, capture_output=True)
    run("init", "-q")
    (repo / "f.txt").write_text("one")
    run("add", "f.txt")
    run("commit", "-q", "-m", "one")
    monkeypatch.setattr(cli, "REPO", repo)
    arm = cli.make_arm("baseline", "rev:HEAD", tmp_path)
    assert arm.repo != repo and (arm.repo / "f.txt").read_text() == "one"
    assert len(arm.revision) >= 7
    cli.drop_worktrees([arm])
    assert not arm.repo.exists()
    with pytest.raises(SystemExit):
        cli.make_arm("baseline", "rev:does-not-exist", tmp_path)


def test_cli_plan_interleaves_arms_so_drift_hits_both():
    cli = _cli()
    arms = [types.SimpleNamespace(name="baseline"), types.SimpleNamespace(name="candidate")]
    order = cli.plan(["x", "y"], arms, 2)
    assert order[:4] == [("baseline", "x", 0), ("candidate", "x", 0), ("baseline", "y", 0), ("candidate", "y", 0)]
    assert len(order) == 8


def test_cli_plan_only_and_unknown_case(capsys):
    cli = _cli()
    assert cli.main(["--plan", "--cases", "bug_fix", "--repeats", "2", "--baseline", "current"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runs"] == 4 and plan["conditions"]["mode"] == "scripted model"
    assert cli.main(["--cases", "nope"]) == 3
    assert cli.main(["--list-cases"]) == 0
    assert "recovery_kill_mid_tool" in capsys.readouterr().out


def test_cli_unreachable_endpoint_is_exit_3_not_a_crash(capsys):
    cli = _cli()
    assert cli.main(["--endpoint", "http://127.0.0.1:9/v1", "--plan"]) == 3
    assert "cannot reach" in capsys.readouterr().err


def test_cli_exit_codes_follow_the_verdict():
    cli = _cli()
    assert cli.EXIT_BY_VERDICT["regression"] == 1 and cli.EXIT_BY_VERDICT["blocked"] == 1
    assert cli.EXIT_BY_VERDICT["inconclusive"] == 2 and cli.EXIT_BY_VERDICT["equivalent"] == 0


def test_cleanup_removes_read_only_files(tmp_path, monkeypatch):
    """Git writes its objects read-only; on Windows a plain rmtree left the
    sandbox behind and every case reported a state leak."""
    import os
    import stat
    monkeypatch.setattr(hp.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "sbx"))
    (tmp_path / "sbx").mkdir()
    sb = hp.Sandbox.create()
    obj = sb.workspace / ".git" / "objects" / "ab" / "cdef"
    obj.parent.mkdir(parents=True)
    obj.write_text("x", encoding="utf-8")
    os.chmod(obj, stat.S_IREAD)
    os.chmod(obj.parent, stat.S_IREAD | stat.S_IEXEC)
    sb.cleanup(pause_s=0.01)
    assert not sb.leftover()


def test_cleanup_ends_a_command_still_running_in_the_sandbox():
    """A command started by a server a case killed keeps running with the workspace
    as its working directory; on Windows that keeps the sandbox from being removed."""
    import subprocess
    import sys as _sys
    box = hp.Sandbox.create()
    proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(box.workspace))
    try:
        box.cleanup()
        assert proc.wait(timeout=10) is not None
        assert not box.leftover()
    finally:
        if proc.poll() is None:
            proc.kill()
