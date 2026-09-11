"""tests/test_cmp13_alternatives.py — W2-G (CMP-13, CONTRATO_CMP_W2.md).

`src/alternatives.py` end to end: isolation (a git worktree for a repo, a
frozen directory snapshot for a plain folder, a text snapshot for a single
document), `compare`'s diff/contested-file detection, and the decisive test
CMP-13's own contract names: two alternatives modify the same file while the
user edits the main copy too -- applying one never destroys the manual edit
and never mixes the other alternative in.

Run: python3 -m pytest tests/test_cmp13_alternatives.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import alternatives  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
OTHER = "mallory"


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


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """`src.alternatives` reads `DATA_DIR` lazily on every call, so patching
    the attribute on `src.constants` (not an already-imported copy) is
    enough to sandbox every experiment this test file creates."""
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    yield


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _write(root / "a.txt", "line1\nline2\nline3\n")
    _write(root / "b.txt", "keep me\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "initial"], root)
    return root


# ---------------------------------------------------------------------------
# Creation / isolation
# ---------------------------------------------------------------------------
def test_create_experiment_git_sha(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "try two fixes", str(repo))
    assert exp["base_kind"] == "git_sha"
    assert exp["workspace"] == os.path.realpath(str(repo))
    assert len(exp["base_ref"]) == 40


def test_create_experiment_needs_a_commit(tmp_path):
    root = tmp_path / "empty_repo"
    root.mkdir()
    _git(["init"], root)
    with pytest.raises(alternatives.InvalidWorkspaceError):
        alternatives.create_experiment(OWNER, "proj1", "goal", str(root))


def test_add_alternative_worktree_is_isolated(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt1 = alternatives.add_alternative(OWNER, exp["id"], "one")
    alt2 = alternatives.add_alternative(OWNER, exp["id"], "two")
    assert alt1["isolation"] == "worktree"
    assert os.path.isdir(alt1["path"])
    assert alt1["path"] != alt2["path"]

    _write(os.path.join(alt1["path"], "a.txt"), "changed by alt1\n")
    # a different alternative, and the main repo, are untouched
    assert _read(os.path.join(alt2["path"], "a.txt")) == "line1\nline2\nline3\n"
    assert _read(os.path.join(repo, "a.txt")) == "line1\nline2\nline3\n"


def test_owner_scoping(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    with pytest.raises(alternatives.ExperimentNotFoundError):
        alternatives.get_experiment(OTHER, exp["id"])


def test_snapshot_dir_isolation_without_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain / "x.txt", "hello\n")
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(plain))
    assert exp["base_kind"] == "snapshot"
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    assert alt["isolation"] == "snapshot_dir"

    _write(os.path.join(alt["path"], "x.txt"), "changed\n")
    assert _read(plain / "x.txt") == "hello\n"  # the main copy is untouched

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert "x.txt" in result["applied_files"]
    assert _read(plain / "x.txt") == "changed\n"


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------
def test_compare_reports_diff_and_contested_files(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt1 = alternatives.add_alternative(OWNER, exp["id"], "one")
    alt2 = alternatives.add_alternative(OWNER, exp["id"], "two")
    _write(os.path.join(alt1["path"], "a.txt"), "line1\nALT1\nline3\n")
    _write(os.path.join(alt2["path"], "a.txt"), "line1\nALT2\nline3\n")

    result = alternatives.compare(OWNER, exp["id"])
    by_id = {a["id"]: a for a in result["alternatives"]}
    assert by_id[alt1["id"]]["diff_summary"]["files_changed"] == 1
    assert by_id[alt2["id"]]["diff_summary"]["files_changed"] == 1
    assert "a.txt" in result["contested_files"]
    assert set(result["contested_files"]["a.txt"]) == {alt1["id"], alt2["id"]}


# ---------------------------------------------------------------------------
# apply_alternative(): fast-forward, real three-way merge, and the decisive
# conflict test.
# ---------------------------------------------------------------------------
def test_apply_fast_forwards_when_main_copy_is_unchanged(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    _write(os.path.join(alt["path"], "a.txt"), "line1\nALT\nline3\n")

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert "a.txt" in result["applied_files"]
    assert _read(os.path.join(repo, "a.txt")) == "line1\nALT\nline3\n"


def test_apply_three_way_merges_non_overlapping_edits(repo):
    """The alternative changed the first line; the user, in the main copy,
    changed the last line of the SAME file -- a real merge, not a
    fast-forward, and both sides survive."""
    _write(repo / "c.txt", "one\ntwo\nthree\nfour\nfive\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "add c"], repo)

    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    _write(os.path.join(alt["path"], "c.txt"), "ONE\ntwo\nthree\nfour\nfive\n")
    _write(os.path.join(repo, "c.txt"), "one\ntwo\nthree\nfour\nFIVE\n")

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert "c.txt" in result["applied_files"]
    assert _read(os.path.join(repo, "c.txt")) == "ONE\ntwo\nthree\nfour\nFIVE\n"


def test_apply_conflict_never_destroys_manual_edit_or_mixes_other_alternative(repo):
    """The decisive test (CMP-13 contract, INFORME §3.12): two alternatives
    touch the SAME line of the same file, and the user hand-edits that same
    line in the main copy while the experiment is running. Applying EITHER
    alternative must refuse (a real collision, not a guessable merge) and
    the main copy's manual edit must survive byte for byte -- and it must
    still survive after trying the OTHER alternative too, proving neither
    apply attempt ever mixed anything in."""
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt1 = alternatives.add_alternative(OWNER, exp["id"], "one")
    alt2 = alternatives.add_alternative(OWNER, exp["id"], "two")
    _write(os.path.join(alt1["path"], "a.txt"), "line1\nALT1\nline3\n")
    _write(os.path.join(alt2["path"], "a.txt"), "line1\nALT2\nline3\n")
    _write(os.path.join(repo, "a.txt"), "line1\nMANUAL EDIT\nline3\n")

    with pytest.raises(alternatives.ApplyConflictError) as excinfo:
        alternatives.apply_alternative(OWNER, exp["id"], alt1["id"])
    assert "a.txt" in excinfo.value.conflicts
    assert _read(os.path.join(repo, "a.txt")) == "line1\nMANUAL EDIT\nline3\n"

    with pytest.raises(alternatives.ApplyConflictError) as excinfo2:
        alternatives.apply_alternative(OWNER, exp["id"], alt2["id"])
    assert "a.txt" in excinfo2.value.conflicts
    assert _read(os.path.join(repo, "a.txt")) == "line1\nMANUAL EDIT\nline3\n"


def test_apply_conflict_writes_nothing_even_for_a_second_untouched_file(repo):
    """A multi-file alternative where ONE file conflicts must not partially
    apply the other, unrelated file either -- all-or-nothing."""
    _write(repo / "d.txt", "d-original\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "add d"], repo)

    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    _write(os.path.join(alt["path"], "a.txt"), "line1\nALT\nline3\n")
    _write(os.path.join(alt["path"], "d.txt"), "d-from-alt\n")
    # user edits the SAME line of a.txt only
    _write(os.path.join(repo, "a.txt"), "line1\nMANUAL\nline3\n")

    with pytest.raises(alternatives.ApplyConflictError):
        alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    # d.txt (no conflict on its own) was NOT written either
    assert _read(os.path.join(repo, "d.txt")) == "d-original\n"


# ---------------------------------------------------------------------------
# combine(): per-file selection across two alternatives
# ---------------------------------------------------------------------------
def test_combine_applies_different_files_from_different_alternatives(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt1 = alternatives.add_alternative(OWNER, exp["id"], "one")
    alt2 = alternatives.add_alternative(OWNER, exp["id"], "two")
    _write(os.path.join(alt1["path"], "a.txt"), "line1\nFROM_ALT1\nline3\n")
    _write(os.path.join(alt2["path"], "b.txt"), "FROM_ALT2\n")

    result = alternatives.combine(OWNER, exp["id"], {"a.txt": alt1["id"], "b.txt": alt2["id"]})
    sources = {(a["path"], a["from"]) for a in result["applied"]}
    assert ("a.txt", alt1["id"]) in sources
    assert ("b.txt", alt2["id"]) in sources
    assert _read(os.path.join(repo, "a.txt")) == "line1\nFROM_ALT1\nline3\n"
    assert _read(os.path.join(repo, "b.txt")) == "FROM_ALT2\n"


# ---------------------------------------------------------------------------
# doc_version isolation (no filesystem workspace at all)
# ---------------------------------------------------------------------------
def test_doc_version_apply_round_trip():
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "base text\n")
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "edited text\n")

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"], mine_doc_content="base text\n")
    assert result["content"] == "edited text\n"


# ---------------------------------------------------------------------------
# run_tests(): a real subprocess, never a shell
# ---------------------------------------------------------------------------
def test_run_tests_records_success(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    command = f'{sys.executable} -c "print(1)"'
    result = alternatives.run_tests(OWNER, exp["id"], alt["id"], command)
    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_run_tests_records_failure_and_alt_status(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    command = f'{sys.executable} -c "import sys; sys.exit(3)"'
    result = alternatives.run_tests(OWNER, exp["id"], alt["id"], command)
    assert result["ok"] is False
    assert result["exit_code"] == 3

    fresh = alternatives.get_experiment(OWNER, exp["id"])
    alt_fresh = next(a for a in fresh["alternatives"] if a["id"] == alt["id"])
    assert alt_fresh["status"] == "failed"


# ---------------------------------------------------------------------------
# delete_experiment()
# ---------------------------------------------------------------------------
def test_delete_experiment_removes_worktree_and_record(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    alt_path = alt["path"]

    alternatives.delete_experiment(OWNER, exp["id"])
    assert not os.path.isdir(alt_path)
    with pytest.raises(alternatives.ExperimentNotFoundError):
        alternatives.get_experiment(OWNER, exp["id"])


# ---------------------------------------------------------------------------
# Contract-wide rule: unknown cost is the literal string, never zero.
# ---------------------------------------------------------------------------
def test_cost_starts_unknown_never_zero(repo):
    exp = alternatives.create_experiment(OWNER, "proj1", "goal", str(repo))
    alt = alternatives.add_alternative(OWNER, exp["id"], "one")
    assert alt["cost"]["known_usd"] == "unknown"
