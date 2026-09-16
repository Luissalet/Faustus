"""`src.skill_sources._rmtree_force` — deleting a `.git` whose pack files
are read-only.

On Windows, git for Windows marks `.git/objects/pack/*.pack`/`*.idx`
read-only on disk; a plain `shutil.rmtree` (or one with
`ignore_errors=True`, which silently leaves the directory behind instead of
failing loudly) raises `PermissionError`/`WinError 5` there. This is the
POSIX-reproducible half of that failure: deletion permission on POSIX is
governed by the CONTAINING DIRECTORY's write bit, not the file's own mode,
so chmod'ing the files alone (as Windows' read-only attribute would) is not
enough to reproduce a real `PermissionError` here — the test also strips
the directory's own write bit, which is what actually makes `shutil.rmtree`
fail on POSIX too, and confirms it does before showing `_rmtree_force`
fixes it.
"""
from __future__ import annotations

import os
import shutil
import stat

import pytest

from src.skill_sources import _rmtree_force


def _make_readonly_git_pack_tree(tmp_path):
    """A `.git/objects/pack/` with read-only pack files inside a read-only
    directory — the structure this whole module strips `.git` off of."""
    git_dir = tmp_path / "clone" / ".git"
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    pack_file = pack_dir / "pack-abcd1234.pack"
    idx_file = pack_dir / "pack-abcd1234.idx"
    pack_file.write_bytes(b"pack-data")
    idx_file.write_bytes(b"idx-data")
    # The file's own read-only bit (what Windows' git actually sets)...
    os.chmod(pack_file, stat.S_IREAD)
    os.chmod(idx_file, stat.S_IREAD)
    # ...and the directory's write bit (what actually blocks unlink() on
    # POSIX, and so is what makes this reproduce for real here).
    os.chmod(pack_dir, stat.S_IREAD | stat.S_IEXEC)
    return git_dir


def test_plain_rmtree_genuinely_fails_on_this_tree(tmp_path):
    """Confirms the fixture reproduces the real bug before showing the fix
    works — otherwise a green test here would prove nothing.

    Skipped under root: the permission bits this fixture relies on (a
    directory's own write bit blocking unlink() inside it) are exactly
    what root bypasses, so this specific confirmation cannot reproduce
    under root — same reason CI/build containers often run tests as a
    non-root user for permission-sensitive cases. The two tests that
    matter for the actual fix (`test_rmtree_force_removes_the_readonly_pack_tree`
    and the no-op/ordinary-tree tests below) do not depend on this and
    still run.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission-bit enforcement is bypassed by root")
    git_dir = _make_readonly_git_pack_tree(tmp_path)
    with pytest.raises(PermissionError):
        shutil.rmtree(git_dir)
    # Restore permissions so pytest's own tmp_path cleanup doesn't also trip
    # over this on teardown.
    for root, dirs, files in os.walk(git_dir):
        for name in dirs:
            os.chmod(os.path.join(root, name), stat.S_IRWXU)
        for name in files:
            os.chmod(os.path.join(root, name), stat.S_IRWXU)


def test_rmtree_force_removes_the_readonly_pack_tree(tmp_path):
    git_dir = _make_readonly_git_pack_tree(tmp_path)
    _rmtree_force(git_dir)
    assert not git_dir.exists()


def test_rmtree_force_is_a_silent_no_op_on_a_missing_path(tmp_path):
    missing = tmp_path / "never-existed"
    _rmtree_force(missing)  # must not raise
    assert not missing.exists()


def test_rmtree_force_removes_an_ordinary_writable_tree_too(tmp_path):
    plain = tmp_path / "plain"
    (plain / "a" / "b").mkdir(parents=True)
    (plain / "a" / "b" / "file.txt").write_text("hi", encoding="utf-8")
    _rmtree_force(plain)
    assert not plain.exists()
