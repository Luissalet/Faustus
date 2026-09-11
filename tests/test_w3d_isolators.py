"""tests/test_w3d_isolators.py — W3-D (CONTRATO_W3.md), CMP-13 follow-up.

`src/branching_futures/isolation.py` used to have exactly ONE
`BranchIsolator` implementation (`InMemoryIsolator`, "no filesystem or
network capability" by its own docstring). This exercises the two real
ones added here (`WorktreeIsolator` over `src.git_panel.worktree_add/
remove`, `SnapshotDirIsolator` by `shutil.copytree`), the `isolator_for`
factory that picks between them (and falls back to `InMemoryIsolator` for a
blank/non-directory workspace), and that `BranchingService` now accepts an
injected isolator without changing anything else about how it behaves.

Run: python3 -m pytest tests/test_w3d_isolators.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.branching_futures import isolation  # noqa: E402
from src.branching_futures.service import BranchingService  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _write(root / "a.txt", "line1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "initial"], root)
    head = _git(["rev-parse", "HEAD"], root).stdout.strip()
    return root, head


# ---------------------------------------------------------------------------
# isolator_for: picks the right class by what `workspace` actually is
# ---------------------------------------------------------------------------
def test_isolator_for_blank_workspace_is_in_memory():
    assert isinstance(isolation.isolator_for(None), isolation.InMemoryIsolator)
    assert isinstance(isolation.isolator_for(""), isolation.InMemoryIsolator)


def test_isolator_for_missing_directory_is_in_memory(tmp_path):
    assert isinstance(isolation.isolator_for(str(tmp_path / "nope")), isolation.InMemoryIsolator)


def test_isolator_for_git_repo_is_worktree(repo):
    root, _head = repo
    assert isinstance(isolation.isolator_for(str(root)), isolation.WorktreeIsolator)


def test_isolator_for_plain_directory_is_snapshot_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert isinstance(isolation.isolator_for(str(plain)), isolation.SnapshotDirIsolator)


# ---------------------------------------------------------------------------
# WorktreeIsolator: real fork/inspect/cleanup over git worktrees
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_isolator_fork_is_a_real_independent_checkout(repo, tmp_path):
    root, head = repo
    iso = isolation.WorktreeIsolator(root=str(tmp_path / "isolated"))
    handle = await iso.fork({"workspace": str(root), "base_ref": head},
                             {"namespace": "branch:f1/b1"})
    assert handle["real_external_effects"] is True
    assert os.path.isdir(handle["path"])
    assert _read(os.path.join(handle["path"], "a.txt")) == "line1\n"

    # Editing the fork must not touch the main checkout, and vice versa.
    _write(os.path.join(handle["path"], "a.txt"), "from branch\n")
    assert _read(os.path.join(root, "a.txt")) == "line1\n"
    _write(os.path.join(root, "a.txt"), "from main\n")
    assert _read(os.path.join(handle["path"], "a.txt")) == "from branch\n"

    info = await iso.inspect(handle)
    assert info["exists"] is True

    result = await iso.cleanup(handle)
    assert result["removed"] is True
    assert not os.path.isdir(handle["path"])
    info_after = await iso.inspect(handle)
    assert info_after["exists"] is False


@pytest.mark.asyncio
async def test_worktree_isolator_rejects_blank_namespace(repo, tmp_path):
    root, head = repo
    iso = isolation.WorktreeIsolator(root=str(tmp_path / "isolated"))
    with pytest.raises(ValueError):
        await iso.fork({"workspace": str(root), "base_ref": head}, {"namespace": ""})


@pytest.mark.asyncio
async def test_worktree_isolator_rejects_non_directory_workspace(tmp_path):
    iso = isolation.WorktreeIsolator(root=str(tmp_path / "isolated"))
    with pytest.raises(ValueError):
        await iso.fork({"workspace": str(tmp_path / "nope"), "base_ref": "HEAD"},
                        {"namespace": "branch:f1/b1"})


# ---------------------------------------------------------------------------
# SnapshotDirIsolator: real fork/inspect/cleanup over a frozen copy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_dir_isolator_fork_is_an_independent_copy(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()
    _write(source / "notes.txt", "original\n")

    iso = isolation.SnapshotDirIsolator(root=str(tmp_path / "isolated"))
    handle = await iso.fork({"workspace": str(source)}, {"namespace": "branch:f2/b1"})
    assert handle["real_external_effects"] is True
    assert _read(os.path.join(handle["path"], "notes.txt")) == "original\n"

    _write(os.path.join(handle["path"], "notes.txt"), "edited in branch\n")
    assert _read(source / "notes.txt") == "original\n"  # the source is untouched

    info = await iso.inspect(handle)
    assert info["exists"] is True
    result = await iso.cleanup(handle)
    assert result["removed"] is True
    assert not os.path.isdir(handle["path"])


@pytest.mark.asyncio
async def test_snapshot_dir_isolator_two_branches_never_collide(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()
    _write(source / "notes.txt", "original\n")
    iso = isolation.SnapshotDirIsolator(root=str(tmp_path / "isolated"))

    handle_a = await iso.fork({"workspace": str(source)}, {"namespace": "branch:f3/a"})
    handle_b = await iso.fork({"workspace": str(source)}, {"namespace": "branch:f3/b"})
    assert handle_a["path"] != handle_b["path"]

    _write(os.path.join(handle_a["path"], "notes.txt"), "A\n")
    _write(os.path.join(handle_b["path"], "notes.txt"), "B\n")
    assert _read(os.path.join(handle_a["path"], "notes.txt")) == "A\n"
    assert _read(os.path.join(handle_b["path"], "notes.txt")) == "B\n"


@pytest.mark.asyncio
async def test_snapshot_dir_isolator_rejects_duplicate_namespace(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()
    _write(source / "notes.txt", "x\n")
    iso = isolation.SnapshotDirIsolator(root=str(tmp_path / "isolated"))
    await iso.fork({"workspace": str(source)}, {"namespace": "branch:dup"})
    with pytest.raises(ValueError):
        await iso.fork({"workspace": str(source)}, {"namespace": "branch:dup"})


# ---------------------------------------------------------------------------
# BranchingService: isolator is injectable without changing anything else
# ---------------------------------------------------------------------------
def test_branching_service_defaults_to_in_memory_isolator():
    service = BranchingService()
    assert isinstance(service.isolator, isolation.InMemoryIsolator)


def test_branching_service_accepts_an_injected_isolator(tmp_path):
    real = isolation.SnapshotDirIsolator(root=str(tmp_path / "isolated"))
    service = BranchingService(isolator=real)
    assert service.isolator is real


def test_branching_service_behaviour_is_unchanged_with_default_isolator():
    """Injecting nothing must behave exactly like before this lote: the
    decisive fairness guarantee (`tests/test_branching_futures.py`) is not
    this file's job to re-prove, but a smoke check that `create` still
    works with the new constructor signature is."""
    from src.durable_feature_store import DurableFeatureStore
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        service = BranchingService(DurableFeatureStore(os.path.join(tmp, "futures.db")))
        future = service.create(owner="alice", request={
            "title": "Choose implementation", "intent": "Find the strongest implementation",
            "project_id": "p1", "session_id": "s1", "mode": "simulate",
            "base_snapshot": {"state_revision": "abc"},
            "strategies": [{"id": "simple", "title": "Simple"}, {"id": "fast", "title": "Fast"}],
            "criteria": [{"name": "quality", "weight": 1, "direction": "max"}],
        })
        assert future["branches"][0]["effect_policy"]["real_external_effects"] is False
