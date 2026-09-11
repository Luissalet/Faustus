"""PLAN-03 (Lote 71): a real delegation's file claims show up in the
process-wide ownership registry, and go away when the worker is released —
so `GET /api/agents/leases` reflects who edits what, not only what a test
wrote there by hand."""
from __future__ import annotations

from types import SimpleNamespace

from src import resource_ownership
from src.agent_tools import subagent_tools


def _run(files):
    return SimpleNamespace(id="run-abc", name="worker A", files=list(files), delegation_id="deleg1")


def test_claim_is_mirrored_and_released():
    resource_ownership.reset_registry()
    reg = resource_ownership.get_registry()
    run = _run(["src/a.py", "src/b.py"])

    subagent_tools._mirror_leases(run, run.files, acquire=True)
    held = {l.resource for l in reg.active_leases()}
    assert {"src/a.py", "src/b.py"} <= held
    lease = next(l for l in reg.active_leases() if l.resource == "src/a.py")
    assert lease.owner == "run-abc"
    assert lease.task_id == "deleg1"

    subagent_tools._mirror_leases(run, run.files, acquire=False)
    assert not {l.resource for l in reg.active_leases()} & {"src/a.py", "src/b.py"}


def test_second_worker_on_same_file_is_a_visible_conflict():
    resource_ownership.reset_registry()
    reg = resource_ownership.get_registry()
    subagent_tools._mirror_leases(_run(["src/a.py"]), ["src/a.py"], acquire=True)
    other = SimpleNamespace(id="run-xyz", name="worker B", files=["src/a.py"], delegation_id="deleg2")
    subagent_tools._mirror_leases(other, ["src/a.py"], acquire=True)
    conflicts = reg.conflicts()
    assert conflicts and conflicts[-1].resource == "src/a.py"
    assert conflicts[-1].requester == "run-xyz"


def test_mirror_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(resource_ownership, "get_registry", boom)
    subagent_tools._mirror_leases(_run(["x"]), ["x"], acquire=True)  # no exception
