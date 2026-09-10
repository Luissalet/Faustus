"""ARCH-02 — who may stop a process, not just whether Faustus may at all.

`can_stop` adds one veto on top of `check()`'s: a process nobody tagged with
`note_started(..., owner=...)` defers to `check()` alone — unchanged
behaviour for every caller written before ARCH-02, which never passes
`owner=`. A process someone DID tag may only be stopped by that same owner:
closing one client must not be able to tear down a shared server or model
runner another client started, even though the process object itself is, by
`check()`'s narrower question ("did Faustus spawn this and still hold it
unreaped?"), still ours to kill.
"""
from __future__ import annotations

import pytest

from src import process_ownership as po


class LiveProc:
    """Stands in for a Popen / asyncio Process we started and still hold."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True


class _NoSuchProcess(Exception):
    pass


class FakeTreeProc:
    """A process the fake OS knows about: a pid, a birth date, its children —
    same shape as `tests/test_process_tree_ownership.py`'s `FakeProc`, reused
    here so the "árbol simulado" the lot asks for is exercised the same way
    `terminate_tree`'s own walk is proven elsewhere, not a second convention."""

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


@pytest.fixture(autouse=True)
def _clean_registry():
    """`_started`/`_owners` are module-level: leaving a record behind would
    leak into whichever test runs next, the same hazard
    `test_process_ownership.py::_no_spawn_records` already guards against."""
    for pid in po.started_pids():
        po.forget(pid)
    yield
    for pid in po.started_pids():
        po.forget(pid)


@pytest.fixture
def fake_os(monkeypatch):
    os_ = FakeOS()
    monkeypatch.setattr(po, "_psutil", lambda: os_)
    return os_


def test_an_untagged_process_defers_entirely_to_check(monkeypatch):
    """No owner was ever recorded: `can_stop` must agree with `check()` in
    every particular — this is the whole backward-compatibility promise for
    every caller that predates ARCH-02 and never passes `owner=`."""
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=9001)
    po.note_started(proc)  # no owner=

    assert po.can_stop(proc, "web") == po.check(proc)
    assert po.can_stop(proc, "anyone at all") == po.check(proc)


def test_the_actor_that_started_it_may_stop_it(monkeypatch):
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=9002)
    po.note_started(proc, owner="desktop")

    assert po.can_stop(proc, "desktop").owned is True


def test_a_different_client_may_not_stop_it_even_though_check_says_owned(monkeypatch):
    """The exact gap ARCH-02 closes: without the owner veto, `check()` alone
    says `owned=True` for ANY caller holding the process object, so a web
    client's disconnect handler could kill a server desktop started."""
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=9003)
    po.note_started(proc, owner="desktop")

    assert po.check(proc).owned is True  # the object-level question still says yes
    verdict = po.can_stop(proc, "web")
    assert verdict.owned is False
    assert verdict.code == "not_your_process"
    assert "desktop" in verdict.reason and "web" in verdict.reason
    assert proc.killed is False  # refusing means nothing was signalled


def test_owner_of_reports_nothing_for_an_untagged_pid(monkeypatch):
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=9004)
    po.note_started(proc)
    assert po.owner_of(proc) == ""


def test_forget_clears_the_owner_tag_along_with_the_spawn_record(monkeypatch):
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=9005)
    po.note_started(proc, owner="desktop")
    po.forget(proc)
    assert po.owner_of(proc) == ""
    assert po.spawn_creation_time(proc.pid) is None


def test_closing_a_web_client_does_not_stop_a_shared_server_tree_desktop_started(
    fake_os, monkeypatch
):
    """The lot's own "test con árbol simulado": a shared server desktop
    started, with a worker child, must survive a *web* client's request to
    stop it — `can_stop` has to refuse BEFORE any signal ever reaches the
    simulated tree `terminate_tree` would otherwise walk and kill. The
    desktop client that actually started it can still take the whole tree
    down, workers included."""
    monkeypatch.setattr(po, "IS_WINDOWS", False)
    FakeTreeProc(fake_os, pid=7000, created=1000.0)
    FakeTreeProc(fake_os, pid=7001, created=1000.5, parent=7000)  # a worker child

    server_proc = LiveProc(pid=7000)
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    po.note_started(server_proc, command="shared server", owner="desktop")

    # A web client asks to stop what it thinks is "its" server.
    verdict = po.can_stop(server_proc, "web")
    assert verdict.owned is False
    assert verdict.code == "not_your_process"
    assert fake_os.killed == []  # refused before any signal reached the tree

    # The desktop client that actually started it may still stop it.
    assert po.can_stop(server_proc, "desktop").owned is True
    outcome = po.terminate_tree(7000, spawned_at=1000.0)
    assert outcome.owned
    assert sorted(outcome.signalled) == [7000, 7001]
    assert sorted(fake_os.killed) == [7000, 7001]
