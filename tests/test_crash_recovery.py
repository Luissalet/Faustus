"""Recovery after a power cut (src/crash_recovery.py).

A power cut does not close files or run a `finally`; what it leaves is a set of
records that stopped being written at the same instant. What is pinned here:
grouping by mtime ALONE, grouping BEFORE filtering (there is a case that only
survives in that order), the three confidence levels, an unknown boot time
doing nothing at all, a plan that re-pins the job's own model and parameters
instead of today's defaults, marking the stale jobs `interrupted` with the
reason, and `verify_resumed` refusing to call anything resumed without a
process-table probe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src import crash_recovery as cr

BOOT = 1_700_000_000.0


def _mirror(tmp_path, job_id, mtime, *, status="running", **over):
    d = tmp_path / "dispatch"
    d.mkdir(exist_ok=True)
    doc = {"id": job_id, "status": status, "workspace": "D:/proj", "model": "qwen3-coder:30b",
           "title": "Workers", "session_id": f"sess-{job_id}",
           "tasks": [{"name": "w1", "instruction": "add apply_tax", "files": [], "model": ""}],
           "parallel": True, "reviewer": False, "max_rounds": 12, "timeout_s": 900,
           "verify": "pytest -q", "verify_scope": "related", "fix_rounds": 2}
    doc.update(over)
    path = d / f"{job_id}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _runlog(tmp_path, session, mtime, *, final=None, label="a long refactor"):
    d = tmp_path / "runs"
    d.mkdir(exist_ok=True)
    lines = [json.dumps({"status": "running", "run_id": "r-" + session, "ts": mtime - 30,
                         "session_id": session, "lane": "local", "label": label}),
             json.dumps({"seq": 1, "ev": 'data: {"delta": "hello"}'})]
    if final:
        lines.append(json.dumps({"status": final, "ts": mtime}))
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# ── boot time ───────────────────────────────────────────────────────────────

def test_boot_time_is_measured_or_none_never_guessed():
    bt = cr.boot_time()
    assert bt is None or (isinstance(bt, float) and 0 < bt < 4_000_000_000)


@pytest.mark.skipif(not os.path.exists("/proc/stat"), reason="no /proc/stat")
def test_linux_boot_time_comes_from_proc_stat_btime():
    assert cr._linux_boot_time() == cr.boot_time()


def test_an_unknown_boot_time_does_nothing_at_all(tmp_path, monkeypatch):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 60)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 59)
    _mirror(tmp_path, "cccccccccccc", BOOT - 58)
    monkeypatch.setattr(cr, "boot_time", lambda: None)
    assert cr.find_interrupted(str(tmp_path)) == []
    report = cr.boot_scan(str(tmp_path))
    assert report["ok"] is False and "unknown boot time" in report["reason"]
    assert report["plan"] == [] and report["marked"] == []
    # and it did not touch a thing
    assert json.loads((tmp_path / "dispatch" / "aaaaaaaaaaaa.json").read_text())["status"] == "running"


# ── grouping by mtime, before filtering ─────────────────────────────────────

def test_the_pocket_is_grouped_by_mtime_alone(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 300)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 298)
    _mirror(tmp_path, "cccccccccccc", BOOT - 296)
    _mirror(tmp_path, "dddddddddddd", BOOT - 7200, status="done")     # an old, unrelated job
    groups = cr.group_by_mtime(cr.scan_records(str(tmp_path)))
    assert [len(g) for g in groups] == [1, 3]
    assert {r["id"] for r in groups[1]} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"}


def test_group_first_filter_after_keeps_the_pocket_the_other_order_would_split(tmp_path):
    """The bridge is a job that finished cleanly 100 s into the pocket. Filter
    to the live records FIRST and the two live ones are 200 s apart — two loose
    clusters, `low` on both. Group first and it is one tight-enough pocket the
    live records really belong to.
    """
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 300)                       # live
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 200, status="done")        # the bridge
    _mirror(tmp_path, "cccccccccccc", BOOT - 100)                       # live
    clusters = cr.find_interrupted(str(tmp_path), boot_time=BOOT, gap_s=120)
    assert len(clusters) == 1
    c = clusters[0]
    assert len(c["members"]) == 3 and {r["id"] for r in c["interrupted"]} == {"aaaaaaaaaaaa", "cccccccccccc"}
    # what filtering first would have produced instead: two groups of one
    live_only = [r for r in cr.scan_records(str(tmp_path)) if r["live"]]
    assert [len(g) for g in cr.group_by_mtime(live_only, gap_s=120)] == [1, 1]


def test_a_record_outside_the_window_is_left_alone(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 7200)                      # older than lookback
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 120)
    clusters = cr.find_interrupted(str(tmp_path), boot_time=BOOT, lookback_s=3600)
    assert [r["id"] for c in clusters for r in c["interrupted"]] == ["bbbbbbbbbbbb"]


def test_a_finished_job_is_never_reported_however_close_it_sits(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 61, status="done")
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 60, status="partial")
    _mirror(tmp_path, "cccccccccccc", BOOT - 59, status="cancelled")
    assert cr.find_interrupted(str(tmp_path), boot_time=BOOT) == []


# ── the three confidences ───────────────────────────────────────────────────

def test_high_confidence_needs_three_records_in_a_tight_pre_boot_pocket(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 42)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 41)
    _runlog(tmp_path, "sess-x", BOOT - 40)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT)[0]
    assert c["confidence"] == "high"
    assert "same instant" in c["reason"] and len(c["interrupted"]) == 3


def test_two_records_are_medium_and_say_why(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 42)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 41)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT)[0]
    assert c["confidence"] == "medium" and "too few records" in c["reason"]


def test_three_records_spread_wide_are_medium_not_high(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 300)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 200)
    _mirror(tmp_path, "cccccccccccc", BOOT - 110)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT, gap_s=120)[0]
    assert c["confidence"] == "medium" and "too spread out" in c["reason"]


def test_one_record_is_low(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 30)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT)[0]
    assert c["confidence"] == "low" and "one file proves nothing" in c["reason"]


def test_a_record_written_after_this_boot_is_low_and_says_it_is_this_process(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT + 30)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT + 31)
    _mirror(tmp_path, "cccccccccccc", BOOT + 32)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT, slack_s=300)[0]
    assert c["confidence"] == "low" and "AFTER this boot" in c["reason"]


def test_a_record_whose_process_is_still_alive_is_not_interrupted(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 30, pid=os.getpid())
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 29)
    clusters = cr.find_interrupted(str(tmp_path), boot_time=BOOT)
    assert [r["id"] for r in clusters[0]["interrupted"]] == ["bbbbbbbbbbbb"]
    assert clusters[0]["still_running"] == ["aaaaaaaaaaaa"]


# ── run logs ────────────────────────────────────────────────────────────────

def test_a_run_log_that_reached_a_terminal_status_is_not_live(tmp_path):
    _runlog(tmp_path, "done-one", BOOT - 30, final="done")
    _runlog(tmp_path, "live-one", BOOT - 29)
    rows = {r["id"]: r for r in cr.scan_records(str(tmp_path))}
    assert rows["done-one"]["live"] is False and rows["live-one"]["live"] is True
    assert rows["live-one"]["title"] == "a long refactor"


def test_a_long_run_log_is_read_from_its_head_and_its_tail(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    path = d / "big.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"status": "running", "run_id": "r1", "ts": 1, "session_id": "big"}) + "\n")
        for i in range(20_000):
            fh.write(json.dumps({"seq": i, "ev": 'data: {"delta": "xxxxxxxxxxxxxxxxxxxx"}'}) + "\n")
        fh.write(json.dumps({"status": "stopped", "ts": 2}) + "\n")
    rec = cr._read_run_log(str(path))
    assert rec["status"] == "stopped" and rec["live"] is False and rec["id"] == "big"


# ── the plan re-pins what the job had ───────────────────────────────────────

def test_the_plan_re_pins_the_jobs_own_model_and_parameters(tmp_path):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 30, model="qwen3.5:9b", workspace="D:/proj/api",
            timeout_s=1200, fix_rounds=2, verify="npm test")
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT)[0]
    plan = cr.resume_plan(c)
    assert len(plan) == 1
    entry = plan[0]
    assert entry["job_id"] == "aaaaaaaaaaaa" and entry["workspace"] == "D:/proj/api"
    assert entry["model"] == "qwen3.5:9b"                      # its own, not a default
    assert entry["params"]["timeout_s"] == 1200 and entry["params"]["fix_rounds"] == 2
    assert entry["params"]["verify"] == "npm test" and entry["params"]["max_rounds"] == 12
    assert entry["params"]["tasks"][0]["instruction"] == "add apply_tax"
    assert "re-pin the model it ran on (qwen3.5:9b)" in entry["why"]


def test_a_record_that_does_not_name_its_model_says_so_instead_of_borrowing_one(tmp_path):
    _runlog(tmp_path, "sess-x", BOOT - 30)
    c = cr.find_interrupted(str(tmp_path), boot_time=BOOT)[0]
    entry = cr.resume_plan(c)[0]
    assert entry["model"] is None
    assert "must be re-pinned by hand" in entry["why"] and "today's default" in entry["why"]
    assert "cannot be continued from its log" in entry["why"]


def test_resume_plan_survives_nonsense():
    assert cr.resume_plan(None) == [] and cr.resume_plan({}) == []
    assert cr.resume_plan({"interrupted": [None, 3]}) == []


# ── nothing is resumed, and nothing is claimed without a probe ──────────────

def test_verify_resumed_will_not_claim_success_without_a_process_table_probe():
    plan = [{"job_id": "aaaaaaaaaaaa", "model": "qwen3.5:9b"}]      # nobody attached a pid
    out = cr.verify_resumed(plan)
    assert out["ok"] is False and out["probed"] == 0
    assert out["entries"][0]["verdict"] == "not_probed" and out["entries"][0]["alive"] is None
    assert "nothing was probed" in out["entries"][0]["why"]
    assert out["unverified"] == ["aaaaaaaaaaaa"] and out["running"] == []


def test_verify_resumed_says_running_only_when_the_pid_answers():
    probes = {1: True, 2: False, 3: None}
    plan = [{"job_id": "a", "pid": 1}, {"job_id": "b", "pid": 2}, {"job_id": "c", "pid": 3}]
    out = cr.verify_resumed(plan, probe=lambda pid: probes[pid])
    assert out["running"] == ["a"] and sorted(out["unverified"]) == ["b", "c"]
    assert [e["verdict"] for e in out["entries"]] == ["running", "gone", "not_probed"]
    assert out["ok"] is False
    ok = cr.verify_resumed([{"job_id": "a", "pid": 1}], probe=lambda pid: True)
    assert ok["ok"] is True and ok["probed"] == 1


def test_a_probe_that_throws_proves_nothing():
    def boom(pid):
        raise OSError("no process table here")
    out = cr.verify_resumed([{"job_id": "a", "pid": 4}], probe=boom)
    assert out["ok"] is False and out["entries"][0]["verdict"] == "not_probed"


def test_verify_resumed_of_nothing_claims_nothing():
    out = cr.verify_resumed([])
    assert out["ok"] is False and "nothing is claimed" in out["summary"]


def test_pid_alive_knows_this_process_and_not_a_free_pid():
    assert cr.pid_alive(os.getpid()) is True
    assert cr.pid_alive(0) is None and cr.pid_alive("x") is None and cr.pid_alive(None) is None


# ── marking, and the boot scan itself ───────────────────────────────────────

def test_the_boot_scan_marks_the_stale_jobs_interrupted_with_the_reason(tmp_path, monkeypatch):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 42)
    _mirror(tmp_path, "bbbbbbbbbbbb", BOOT - 41, status="verifying")
    _mirror(tmp_path, "cccccccccccc", BOOT - 40, status="queued")
    _mirror(tmp_path, "dddddddddddd", BOOT - 7200, status="done")
    monkeypatch.setattr(cr, "boot_time", lambda: BOOT)
    report = cr.boot_scan(str(tmp_path))
    assert report["ok"] is True and sorted(report["marked"]) == ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
    assert report["clusters"][0]["confidence"] == "high"
    assert len(report["plan"]) == 3 and all(e["model"] == "qwen3-coder:30b" for e in report["plan"])
    doc = json.loads((tmp_path / "dispatch" / "aaaaaaaaaaaa.json").read_text(encoding="utf-8"))
    assert doc["status"] == "interrupted"
    assert "same instant" in doc["interrupted_reason"]
    assert "re-dispatch" in doc["verdict"]
    # untouched: the old finished job
    assert json.loads((tmp_path / "dispatch" / "dddddddddddd.json").read_text())["status"] == "done"


def test_a_marked_mirror_still_loads_as_an_interrupted_job_in_dispatch(tmp_path, monkeypatch):
    from src import dispatch
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 42)
    monkeypatch.setattr(cr, "boot_time", lambda: BOOT)
    cr.boot_scan(str(tmp_path))
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    dispatch.reset_for_tests()
    job = dispatch.get("aaaaaaaaaaaa")
    assert job is not None and job.status == "interrupted"
    assert "re-dispatch" in (job.verdict or "")
    dispatch.reset_for_tests()


def test_the_boot_scan_does_nothing_at_all_with_the_setting_off(tmp_path, monkeypatch):
    _mirror(tmp_path, "aaaaaaaaaaaa", BOOT - 42)
    monkeypatch.setattr(cr, "boot_time", lambda: BOOT)
    monkeypatch.setattr(cr, "enabled", lambda: False)
    report = cr.boot_scan(str(tmp_path))
    assert report == {"ok": False, "reason": "disabled (agent_crash_recovery)", "boot_time": None,
                      "clusters": [], "plan": [], "marked": []}
    assert json.loads((tmp_path / "dispatch" / "aaaaaaaaaaaa.json").read_text())["status"] == "running"


def test_the_boot_scan_never_raises_and_never_blocks_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "boot_time", lambda: BOOT)
    # no data dir at all
    assert cr.boot_scan(str(tmp_path / "nothing-here"))["plan"] == []
    # unreadable junk instead of records
    d = tmp_path / "dispatch"
    d.mkdir()
    (d / "zzzzzzzzzzzz.json").write_text("{not json", encoding="utf-8")
    (d / "notes.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "x.jsonl").write_text("\x00\x00\x00", encoding="utf-8")
    assert cr.boot_scan(str(tmp_path))["ok"] is True
    # and a scan whose grouping explodes still answers a report
    monkeypatch.setattr(cr, "scan_records", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cr.find_interrupted(str(tmp_path), boot_time=BOOT) == []


def test_the_app_runs_the_boot_scan_at_startup_off_the_critical_path():
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "from src import crash_recovery as _crash_recovery" in src
    assert "asyncio.to_thread(_crash_recovery.boot_scan)" in src
    block = src.split("from src import crash_recovery as _crash_recovery", 1)[1][:600]
    assert "except Exception" in block and "Crash-recovery scan skipped" in block
