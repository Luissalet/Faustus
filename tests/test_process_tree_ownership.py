"""A timeout must not be able to kill somebody else's process (B-001).

`bg_jobs` launches detached children on purpose — a `#!bg pip install` has to
survive the request that started it, and the reaping decision is taken later by
a poll loop reading a JSON file that a *previous* interpreter may have written.
By then the only thing left of the child is a number, and a number is on loan:
once the OS reaps a process its pid goes back in the pool, and the next holder
on this machine is plausibly the operator's Ollama with two GPUs of models
resident. These tests pin the two halves of the answer: the root is signalled
only when the creation time recorded at spawn still matches, and the tree is
walked one generation at a time so that a stranger re-parented onto a recycled
pid is refused instead of taken down with `taskkill /T`.

Both operating systems are covered, because both spellings are unconditional
and the Windows one is the worse of the two: Windows never clears the parent
pid of an orphan, so its recorded parentage outlives the parent itself.
"""
import time

import pytest

from src import bg_jobs
from src import process_ownership as po


class _NoSuchProcess(Exception):
    pass


class FakeProc:
    """A process the fake OS knows about: a pid, a birth date, its children."""

    def __init__(self, table, pid, created, parent=None):
        self.pid = pid
        self._created = created
        self._table = table
        table.procs[pid] = self
        self._children = []
        if parent is not None:
            table.procs[parent]._children.append(self)

    def create_time(self):
        return self._created

    def children(self):
        return list(self._children)

    def name(self):
        return f"p{self.pid}.exe"

    def kill(self):
        self._table.killed.append(self.pid)


class FakeOS:
    """Just enough psutil for the walk: a pid table and a kill log."""

    NoSuchProcess = _NoSuchProcess

    def __init__(self):
        self.procs = {}
        self.killed = []

    def Process(self, pid):
        try:
            return self.procs[int(pid)]
        except KeyError:
            raise _NoSuchProcess(pid)


@pytest.fixture
def fake_os(monkeypatch):
    os_ = FakeOS()
    monkeypatch.setattr(po, "_psutil", lambda: os_)
    return os_


# ── the root ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_a_recycled_pid_is_never_signalled(fake_os, monkeypatch, windows):
    """The pid is alive and it is the number we stored — and it is not ours."""
    monkeypatch.setattr(po, "IS_WINDOWS", windows)
    FakeProc(fake_os, pid=4321, created=9000.0)

    outcome = po.terminate_tree(4321, spawned_at=1000.0)

    assert outcome.code == "recycled"
    assert fake_os.killed == []
    assert "creation time" in outcome.reason


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_a_process_we_really_spawned_is_killed(fake_os, monkeypatch, windows):
    monkeypatch.setattr(po, "IS_WINDOWS", windows)
    FakeProc(fake_os, pid=4321, created=1000.0)

    outcome = po.terminate_tree(4321, spawned_at=1000.25)

    assert outcome.owned and outcome.signalled == (4321,)
    assert fake_os.killed == [4321]


def test_nothing_is_signalled_without_a_recorded_creation_time(fake_os):
    """A record written before this release has nothing to compare against, so
    ownership cannot be shown — and unprovable ownership means no signal."""
    FakeProc(fake_os, pid=4321, created=1000.0)

    outcome = po.terminate_tree(4321, spawned_at=None)

    assert outcome.code == "ownership_unknown"
    assert fake_os.killed == []


def test_a_pid_nobody_holds_any_more_is_reported_gone(fake_os):
    outcome = po.terminate_tree(4321, spawned_at=1000.0)
    assert outcome.code == "gone"
    assert fake_os.killed == []


def test_without_psutil_a_persisted_pid_is_refused_outright(monkeypatch):
    """No psutil means no creation time to compare, on a path that holds no
    process object either. Refuse rather than signal a number."""
    monkeypatch.setattr(po, "_psutil", lambda: None)
    calls = []
    monkeypatch.setattr(po.os, "kill", lambda *a: calls.append(a), raising=False)
    monkeypatch.setattr(po.os, "killpg", lambda *a: calls.append(a), raising=False)

    outcome = po.terminate_tree(4321, spawned_at=1000.0)

    assert outcome.code == "ownership_unknown"
    assert calls == []


# ── the tree ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_true_descendants_die_with_the_root_leaves_first(fake_os, monkeypatch, windows):
    monkeypatch.setattr(po, "IS_WINDOWS", windows)
    FakeProc(fake_os, pid=100, created=1000.0)
    FakeProc(fake_os, pid=101, created=1001.0, parent=100)
    FakeProc(fake_os, pid=102, created=1002.0, parent=101)

    outcome = po.terminate_tree(100, spawned_at=1000.0)

    assert outcome.owned
    assert set(outcome.signalled) == {100, 101, 102}
    # The root goes last: killing it first re-parents its children, and a
    # re-parented child is exactly what the walk below refuses to touch.
    assert fake_os.killed[-1] == 100
    assert fake_os.killed.index(102) < fake_os.killed.index(101)


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_a_stranger_reparented_onto_our_pid_is_spared(fake_os, monkeypatch, windows):
    """The Windows residual, and the reason `taskkill /T` is not used: pid 200's
    real parent died long ago and its recorded parent pid was recycled into
    ours. It predates our process, so it cannot descend from it."""
    monkeypatch.setattr(po, "IS_WINDOWS", windows)
    FakeProc(fake_os, pid=100, created=1000.0)
    FakeProc(fake_os, pid=200, created=500.0, parent=100)     # the operator's server
    FakeProc(fake_os, pid=201, created=600.0, parent=200)     # and its worker

    outcome = po.terminate_tree(100, spawned_at=1000.0)

    assert fake_os.killed == [100]
    assert [pid for pid, _why in outcome.rejected] == [200]
    # Rejecting 200 must not merely skip it: its own subtree is not expanded,
    # or the walk would take 201 down while sparing its parent.
    assert 201 not in fake_os.killed
    assert "recycled" in outcome.rejected[0][1]


def test_windows_never_shells_out_to_taskkill_when_the_tree_can_be_walked(
    fake_os, monkeypatch
):
    import subprocess

    monkeypatch.setattr(po, "IS_WINDOWS", True)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ran.append(a))
    FakeProc(fake_os, pid=100, created=1000.0)
    FakeProc(fake_os, pid=101, created=1001.0, parent=100)

    po.terminate_tree(100, spawned_at=1000.0)

    assert ran == [], "taskkill /T decides parentage from recorded pids alone"


def test_posix_also_signals_the_recorded_process_group(fake_os, monkeypatch):
    """A grandchild that re-parented to init has fallen off the child walk but
    is still in the group its setsid'd ancestor created."""
    monkeypatch.setattr(po, "IS_WINDOWS", False)
    FakeProc(fake_os, pid=100, created=1000.0)
    groups = []
    monkeypatch.setattr(po.os, "getpgid", lambda pid: 100 if pid == 100 else 7, raising=False)
    monkeypatch.setattr(po.os, "killpg", lambda pgid, sig: groups.append((pgid, sig)), raising=False)

    outcome = po.terminate_tree(100, spawned_at=1000.0, pgid=100)

    assert outcome.group_signalled == 100
    assert groups == [(100, po._KILL_SIGNAL)]


def test_posix_will_not_signal_a_group_that_no_longer_matches(fake_os, monkeypatch):
    monkeypatch.setattr(po, "IS_WINDOWS", False)
    FakeProc(fake_os, pid=100, created=1000.0)
    groups = []
    monkeypatch.setattr(po.os, "getpgid", lambda pid: 55, raising=False)
    monkeypatch.setattr(po.os, "killpg", lambda pgid, sig: groups.append((pgid, sig)), raising=False)

    outcome = po.terminate_tree(100, spawned_at=1000.0, pgid=100)

    assert outcome.group_signalled is None
    assert groups == []


# ── the agent shell's teardown goes through the same walk ───────────────────

def test_the_agent_shell_teardown_spares_a_reparented_stranger(fake_os, monkeypatch):
    """`_kill_tree` holds the live process object, so the ROOT needs no further
    proof — but the descendants are still decided by creation time, not by the
    parent pid Windows never clears."""
    from src.agent_tools import subprocess_tools as st

    class LiveProc:
        pid = 100
        returncode = None

        def kill(self):
            fake_os.killed.append("proc.kill")

    monkeypatch.setattr(st, "IS_WINDOWS", True)
    monkeypatch.setattr(po, "IS_WINDOWS", True)
    FakeProc(fake_os, pid=100, created=1000.0)
    FakeProc(fake_os, pid=101, created=1001.0, parent=100)     # ours
    FakeProc(fake_os, pid=200, created=500.0, parent=100)      # not ours

    assert st._kill_tree(LiveProc()) is None

    assert 101 in fake_os.killed and 100 in fake_os.killed
    assert 200 not in fake_os.killed


# ── what bg_jobs persists, and what it does with it ─────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "bg_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    # The seeded pids belong to the fake OS, not to this machine: without this
    # every seeded job would be reconciled as "died" before the branch under
    # test is reached.
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: True)
    return jobs_dir


def _seed(job_id="job0001", pid=4321, created=1000.0, age_s=99999.0, **extra):
    rec = {
        "id": job_id, "session_id": "sess-a", "command": "sleep 60",
        "status": "running", "pid": pid,
        "started_at": time.time() - age_s,
        "ended_at": None, "exit_code": None,
        "max_runtime_s": 10, "followed_up": False,
        "log_path": str(bg_jobs._JOBS_DIR / f"{job_id}.log"),
        "exit_path": str(bg_jobs._JOBS_DIR / f"{job_id}.exit"),
    }
    if created is not None:
        rec["pid_created_at"] = created
    rec.update(extra)
    jobs = bg_jobs._load()
    jobs[job_id] = rec
    bg_jobs._save(jobs)
    return rec


def test_launch_persists_the_identity_a_restart_needs(store, monkeypatch):
    """Restarting Faustus must not lose the evidence: the proof of ownership
    lives in the same JSON file as the pid, not in this interpreter's memory."""
    class _FakePopen:
        pid = 4321

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **kw: _FakePopen())
    monkeypatch.setattr(bg_jobs.process_ownership, "creation_time", lambda pid: 1234.5)
    monkeypatch.setattr(bg_jobs.process_ownership, "process_group_id", lambda pid: 777)

    rec = bg_jobs.launch("sleep 60", session_id="sess-a")

    reloaded = bg_jobs._load()[rec["id"]]
    assert reloaded["pid"] == 4321
    assert reloaded["pid_created_at"] == 1234.5
    assert reloaded["pgid"] == 777
    assert reloaded["id"] == rec["id"], "the run identifier is the record key"


def test_a_timed_out_job_whose_pid_was_recycled_is_not_killed(store, fake_os):
    FakeProc(fake_os, pid=4321, created=9000.0)      # somebody else's process now
    _seed(created=1000.0)

    jobs = bg_jobs.refresh()
    rec = jobs["job0001"]

    assert fake_os.killed == [], "a recycled pid must never be signalled"
    assert rec["status"] == "failed" and rec["timed_out"] is True
    assert "reused" in rec["kill_refused"]


def test_a_timed_out_job_we_still_own_is_killed_with_its_tree(store, fake_os):
    FakeProc(fake_os, pid=4321, created=1000.0)
    FakeProc(fake_os, pid=4322, created=1001.0, parent=4321)
    _seed(created=1000.0)

    bg_jobs.refresh()

    assert set(fake_os.killed) == {4321, 4322}


def test_a_record_from_before_this_release_is_marked_ownership_unknown(store, fake_os):
    """No creation time was stored, so nothing can prove the pid is still ours."""
    FakeProc(fake_os, pid=4321, created=1000.0)
    _seed(created=None)

    rec = bg_jobs.refresh()["job0001"]

    assert fake_os.killed == []
    assert rec["ownership_unknown"] is True
    assert rec["status"] == "failed" and rec["timed_out"] is True


def test_the_followup_says_the_process_was_not_signalled(store, fake_os):
    """The agent has to tell "stopped" from "given up on": in the second case
    the command is very likely still running."""
    FakeProc(fake_os, pid=4321, created=9000.0)
    _seed(created=1000.0)

    text = bg_jobs.result_text(bg_jobs.refresh()["job0001"])

    assert "NOT signalled" in text
    assert "4321" in text


def test_an_explicit_kill_that_was_refused_does_not_claim_the_job_was_killed(
    store, fake_os
):
    FakeProc(fake_os, pid=4321, created=9000.0)
    _seed(created=1000.0, age_s=1.0)     # young: the timeout branch is not it

    rec = bg_jobs.kill("job0001")

    assert fake_os.killed == []
    assert rec["killed"] is False
    assert rec["status"] == "failed", "the record must not stay running forever"
    assert rec["followed_up"] is True
