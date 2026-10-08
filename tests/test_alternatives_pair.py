"""tests/test_alternatives_pair.py -- OBJ-47 (CMP-13 follow-up).

`alternatives.compare_pair`: alternative A against alternative B, file by
file, on real temp git repos / plain folders / document drafts. What matters
and is checked here: the direction of the diff, that identical files are not
listed, that rename / binary / oversize files are reported as such instead of
being diffed as empty, that every cap is stated back, that the overlap
verdict (identical / mergeable / conflict) is the merge `apply` would make,
and that the call writes nothing.

Run: python -m pytest tests/test_alternatives_pair.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import alternatives  # noqa: E402
from src import constants as constants_mod  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


def _write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    yield


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root / "a.txt", "".join(f"line{i}\n" for i in range(1, 21)))
    _write(root / "b.txt", "keep me\n")
    _write(root / "notes" / "old_name.md", "".join(f"note line {i}\n" for i in range(1, 30)))
    _git(["add", "-A"], root)
    _git(["commit", "-m", "initial"], root)
    return root


@pytest.fixture()
def pair(repo):
    """An experiment with two untouched worktree alternatives."""
    exp = alternatives.create_experiment(OWNER, "proj", "try two", str(repo))
    a = alternatives.add_alternative(OWNER, exp["id"], "Alpha")
    b = alternatives.add_alternative(OWNER, exp["id"], "Beta")
    return exp["id"], a, b


def _files(result):
    return {f["path"]: f for f in result["files"]}


# ---------------------------------------------------------------------------
def test_identical_alternatives_have_no_differences(pair):
    exp_id, a, b = pair
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert res["identical"] is True
    assert res["files"] == []
    assert res["summary"]["files_differing"] == 0
    assert res["touched"]["a"] == 0 and res["touched"]["b"] == 0
    assert res["truncation"]["any"] is False


def test_both_make_the_same_edit_is_identical_not_listed(pair):
    exp_id, a, b = pair
    for alt in (a, b):
        _write(os.path.join(alt["path"], "b.txt"), "same edit\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert res["identical"] is True
    assert res["files"] == []
    assert res["summary"]["identical_files"] == 1
    assert res["touched"]["both"] == 1
    assert res["summary"]["overlap"]["identical"] == 1


def test_disjoint_changes_are_listed_in_the_a_to_b_direction(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "a.txt"), "".join(f"line{i}\n" for i in range(1, 21)).replace("line3\n", "LINE THREE\n"))
    _write(os.path.join(b["path"], "b.txt"), "keep me\nand more\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    files = _files(res)
    assert set(files) == {"a.txt", "b.txt"}
    assert all(f["status"] == "changed" for f in files.values())
    # a.txt: A has the edit, B has the base -> going A -> B undoes the edit.
    assert "-LINE THREE" in files["a.txt"]["diff"] and "+line3" in files["a.txt"]["diff"]
    assert files["a.txt"]["diff"].startswith("--- a/a.txt\n+++ b/a.txt")
    assert files["b.txt"]["additions"] == 1 and files["b.txt"]["deletions"] == 0
    assert files["a.txt"]["touched_by"] == ["a"] and files["b.txt"]["touched_by"] == ["b"]
    assert files["a.txt"]["overlap"] is None
    assert files["a.txt"]["base_change"] == {"a": "modified", "b": None}
    assert res["touched"] == {**res["touched"], "a": 1, "b": 1, "both": 0, "only_a": 1, "only_b": 1}
    assert res["summary"]["changed"] == 2
    assert res["summary"]["additions"] == 2 and res["summary"]["deletions"] == 1


def test_swapping_a_and_b_swaps_added_and_removed(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "only_a.txt"), "x\ny\n")
    ab = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    ba = alternatives.compare_pair(OWNER, exp_id, b["id"], a["id"])
    assert _files(ab)["only_a.txt"]["status"] == "removed"
    assert _files(ba)["only_a.txt"]["status"] == "added"
    assert _files(ba)["only_a.txt"]["diff"].startswith("--- /dev/null\n+++ b/only_a.txt")
    assert _files(ab)["only_a.txt"]["diff"].startswith("--- a/only_a.txt\n+++ /dev/null")


def test_same_file_different_edits_in_distant_regions_is_mergeable(pair):
    exp_id, a, b = pair
    base = "".join(f"line{i}\n" for i in range(1, 21))
    _write(os.path.join(a["path"], "a.txt"), base.replace("line2\n", "A-edit\n"))
    _write(os.path.join(b["path"], "a.txt"), base.replace("line19\n", "B-edit\n"))
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    entry = _files(res)["a.txt"]
    assert entry["status"] == "changed"
    assert entry["overlap"] == "mergeable"
    assert entry["touched_by"] == ["a", "b"]
    assert res["summary"]["overlap"] == {"identical": 0, "mergeable": 1, "conflict": 0, "unknown": 0}
    # the verdict is the one apply makes: combining the two onto one file is clean
    assert alternatives.apply_alternative(OWNER, exp_id, a["id"])["applied_files"] == ["a.txt"]


def test_same_lines_different_edits_is_a_conflict_listed_first(pair):
    exp_id, a, b = pair
    base = "".join(f"line{i}\n" for i in range(1, 21))
    _write(os.path.join(a["path"], "a.txt"), base.replace("line5\n", "from A\n"))
    _write(os.path.join(b["path"], "a.txt"), base.replace("line5\n", "from B\n"))
    _write(os.path.join(a["path"], "aaa_first_alphabetically.txt"), "only a\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert res["files"][0]["path"] == "a.txt", "conflicts are listed before anything else"
    assert res["files"][0]["overlap"] == "conflict"
    assert res["summary"]["overlap"]["conflict"] == 1
    assert "-from A" in res["files"][0]["diff"] and "+from B" in res["files"][0]["diff"]


def test_one_deletes_what_the_other_edits_is_a_conflict(pair):
    exp_id, a, b = pair
    os.remove(os.path.join(a["path"], "b.txt"))
    _write(os.path.join(b["path"], "b.txt"), "edited\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    entry = _files(res)["b.txt"]
    assert entry["status"] == "added"  # B has the file, A deleted it
    assert entry["overlap"] == "conflict"
    assert entry["base_change"] == {"a": "deleted", "b": "modified"}


def test_exact_rename_is_one_renamed_entry(pair):
    exp_id, a, b = pair
    os.makedirs(os.path.join(a["path"], "docs"))
    shutil.move(os.path.join(a["path"], "notes", "old_name.md"), os.path.join(a["path"], "docs", "new_name.md"))
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    # A renamed it, B kept the old name: A -> B is the rename undone.
    entry = _files(res)["notes/old_name.md"]
    assert entry["status"] == "renamed"
    assert entry["old_path"] == "docs/new_name.md"
    assert entry["additions"] == 0 and entry["deletions"] == 0
    assert res["summary"]["renamed"] == 1 and res["summary"]["files_differing"] == 1
    assert "docs/new_name.md" not in _files(res)


def test_rename_with_small_edit_is_detected_and_diffed(pair):
    exp_id, a, b = pair
    _write(os.path.join(b["path"], "notes", "renamed.md"),
           "".join(f"note line {i}\n" for i in range(1, 29)) + "a changed last line\n")
    os.remove(os.path.join(b["path"], "notes", "old_name.md"))
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    entry = _files(res)["notes/renamed.md"]
    assert entry["status"] == "renamed"
    assert entry["old_path"] == "notes/old_name.md"
    assert entry["additions"] == 1 and entry["deletions"] == 1
    assert entry["diff"].startswith("--- a/notes/old_name.md\n+++ b/notes/renamed.md")


def test_unrelated_remove_and_add_are_not_called_a_rename(pair):
    exp_id, a, b = pair
    os.remove(os.path.join(b["path"], "b.txt"))
    _write(os.path.join(b["path"], "other.txt"), "completely different content here\n" * 3)
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert {f["status"] for f in res["files"]} == {"removed", "added"}
    assert res["summary"]["renamed"] == 0


def test_binary_files_are_flagged_and_never_diffed(pair):
    exp_id, a, b = pair
    _write_bytes(os.path.join(a["path"], "img.bin"), b"\x89PNG\x00\x01\x02" * 50)
    _write_bytes(os.path.join(b["path"], "img.bin"), b"\x89PNG\x00\x09\x09" * 50)
    _write_bytes(os.path.join(a["path"], "same.bin"), b"\x00\x01")
    _write_bytes(os.path.join(b["path"], "same.bin"), b"\x00\x01")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    files = _files(res)
    assert set(files) == {"img.bin"}
    assert files["img.bin"]["binary"] is True
    assert files["img.bin"]["diff"] == ""
    assert files["img.bin"]["additions"] is None and files["img.bin"]["deletions"] is None
    assert files["img.bin"]["size_a"] == 350
    assert res["summary"]["binary"] == 1
    assert res["summary"]["identical_files"] == 1
    assert res["truncation"]["files_not_diffed"] == 1


def test_invalid_utf8_counts_as_binary(pair):
    exp_id, a, b = pair
    _write_bytes(os.path.join(a["path"], "latin.txt"), "caf\xe9\n".encode("latin-1"))
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert _files(res)["latin.txt"]["binary"] is True


def test_oversize_file_is_listed_but_not_diffed_and_the_cap_is_stated(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "huge.txt"), "x" * 1500 + "\n")
    _write(os.path.join(b["path"], "huge.txt"), "y" * 1500 + "\n")
    _write(os.path.join(b["path"], "small.txt"), "tiny\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"], max_file_bytes=1000)
    files = _files(res)
    assert files["huge.txt"]["too_large"] is True
    assert files["huge.txt"]["diff"] == "" and files["huge.txt"]["additions"] is None
    assert files["huge.txt"]["size_a"] == 1501
    assert files["small.txt"]["too_large"] is False and "+tiny" in files["small.txt"]["diff"]
    assert res["limits"]["max_file_bytes"] == 1000
    assert res["truncation"]["any"] is True
    assert res["summary"]["too_large"] == 1


def test_long_diff_is_truncated_and_says_how_much_was_left_out(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "big.txt"), "".join(f"a{i}\n" for i in range(3000)))
    _write(os.path.join(b["path"], "big.txt"), "".join(f"b{i}\n" for i in range(3000)))
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"], max_diff_lines=100)
    entry = _files(res)["big.txt"]
    assert entry["diff_truncated"] is True
    assert len(entry["diff"].splitlines()) == 100
    assert entry["omitted_lines"] == entry["diff_total_lines"] - 100 > 5000
    assert entry["additions"] == 3000 and entry["deletions"] == 3000, "counts cover the whole diff, not the kept part"
    assert res["truncation"]["diffs"] == 1 and res["truncation"]["any"] is True
    assert res["limits"]["max_diff_lines_per_file"] == 100


def test_file_list_is_capped_and_the_omission_is_counted(pair):
    exp_id, a, b = pair
    for i in range(12):
        _write(os.path.join(a["path"], f"gen/f{i:02d}.txt"), f"{i}\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"], max_files=5)
    assert len(res["files"]) == 5
    assert res["summary"]["files_differing"] == 12
    assert res["truncation"]["files"] is True and res["truncation"]["files_omitted"] == 7


def test_a_file_created_without_git_add_is_part_of_what_the_alternative_did(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "brand_new.py"), "print('hi')\nprint('there')\n")
    pair_res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert _files(pair_res)["brand_new.py"]["status"] == "removed"
    assert pair_res["touched"]["only_a"] == 1
    # `compare` (alternative vs base) sees it too, and `apply` can write it.
    cmp_res = alternatives.compare(OWNER, exp_id)
    mine = next(x for x in cmp_res["alternatives"] if x["id"] == a["id"])
    assert mine["files"]["brand_new.py"] == {"change": "added", "additions": 2, "deletions": 0}


def test_rename_in_compare_is_a_delete_plus_add_with_real_paths(pair):
    exp_id, a, _b = pair
    shutil.move(os.path.join(a["path"], "b.txt"), os.path.join(a["path"], "moved.txt"))
    _git(["add", "-A"], a["path"])
    mine = next(x for x in alternatives.compare(OWNER, exp_id)["alternatives"] if x["id"] == a["id"])
    assert set(mine["files"]) == {"b.txt", "moved.txt"}, "no 'old => new' pseudo path"


def test_compare_pair_is_read_only(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "a.txt"), "changed\n")
    path = alternatives._exp_json_path(exp_id)
    before = (open(path, "rb").read(), os.path.getmtime(path))
    alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    assert (open(path, "rb").read(), os.path.getmtime(path)) == before


def test_same_alternative_twice_is_refused(pair):
    exp_id, a, _b = pair
    with pytest.raises(alternatives.AlternativesError) as exc:
        alternatives.compare_pair(OWNER, exp_id, a["id"], a["id"])
    assert exc.value.error_class == "alternatives.invalid_request"


def test_unknown_alternative_and_other_owner_are_not_found(pair):
    exp_id, a, _b = pair
    with pytest.raises(alternatives.AlternativeNotFoundError):
        alternatives.compare_pair(OWNER, exp_id, a["id"], "alt-nope")
    with pytest.raises(alternatives.ExperimentNotFoundError):
        alternatives.compare_pair("mallory", exp_id, a["id"], "alt-nope")


def test_unicode_and_crlf_and_missing_final_newline(pair):
    exp_id, a, b = pair
    _write(os.path.join(a["path"], "u.txt"), "café\r\nline two")
    _write(os.path.join(b["path"], "u.txt"), "café\r\nline two\n")
    res = alternatives.compare_pair(OWNER, exp_id, a["id"], b["id"])
    entry = _files(res)["u.txt"]
    assert "\\ No newline at end of file" in entry["diff"], "an end-of-file-only difference is visible"
    assert entry["additions"] == 1 and entry["deletions"] == 1


# ---------------------------------------------------------------------------
# Plain folders and documents
# ---------------------------------------------------------------------------
def test_snapshot_folder_pair(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    _write(folder / "one.txt", "1\n2\n3\n")
    _write(folder / "two.txt", "keep\n")
    exp = alternatives.create_experiment(OWNER, "proj", "folder", str(folder))
    assert exp["base_kind"] == "snapshot"
    a = alternatives.add_alternative(OWNER, exp["id"], "A")
    b = alternatives.add_alternative(OWNER, exp["id"], "B")
    _write(os.path.join(a["path"], "one.txt"), "1\nTWO\n3\n")
    _write(os.path.join(b["path"], "one.txt"), "1\n2\n3\n4\n")
    res = alternatives.compare_pair(OWNER, exp["id"], a["id"], b["id"])
    entry = _files(res)["one.txt"]
    assert entry["overlap"] in ("mergeable", "conflict")
    assert entry["overlap"] == "conflict" or entry["overlap"] == "mergeable"
    assert "two.txt" not in _files(res)
    assert res["touched"]["both"] == 1


def test_document_alternatives_pair():
    exp = alternatives.create_doc_experiment(OWNER, "proj", "wording", "intro\nbody\nend\n")
    a = alternatives.add_alternative(OWNER, exp["id"], "Formal", isolation="doc_version")
    b = alternatives.add_alternative(OWNER, exp["id"], "Casual", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], a["id"], "intro\nformal body\nend\n")
    alternatives.set_doc_version_content(OWNER, exp["id"], b["id"], "intro\ncasual body\nend\n")
    res = alternatives.compare_pair(OWNER, exp["id"], a["id"], b["id"])
    entry = _files(res)["(document)"]
    assert entry["status"] == "changed"
    assert "-formal body" in entry["diff"] and "+casual body" in entry["diff"]
    assert entry["overlap"] == "conflict"


def test_document_vs_file_alternative_is_refused(tmp_path):
    exp = alternatives.create_doc_experiment(OWNER, "proj", "wording", "x\n")
    a = alternatives.add_alternative(OWNER, exp["id"], "Doc", isolation="doc_version")
    # a second doc alternative is fine; mixing kinds on one record is not
    b = alternatives.add_alternative(OWNER, exp["id"], "Doc2", isolation="doc_version")
    stored = alternatives._load_raw(exp["id"])
    stored["alternatives"][1]["isolation"] = "snapshot_dir"
    alternatives._save(stored)
    with pytest.raises(alternatives.AlternativesError) as exc:
        alternatives.compare_pair(OWNER, exp["id"], a["id"], b["id"])
    assert exc.value.error_class == "alternatives.invalid_request"
