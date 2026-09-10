"""VER-02 — run_verifier: one deterministic front door for tests/lint/build/
typecheck, always separating a NEW failure from one that predates the patch
(and, additively, what the patch actually fixed).

Follows the same real-pytest-twice pattern as
tests/qa/test_qa_20_fallo_preexistente.py: `workspace_checkpoints.export_tree`
is substituted with a real folder copy (there is no git repo to export from
in a tmp_path), but the command execution and the comparison logic are real.
"""
import shutil
import sys
import textwrap

import pytest

from src import project_tests
from src import verification as ver
from src import workspace_checkpoints as wc

# No "-x": run_verifier is meant to see every failure, not stop at the first
# one the way the interactive-turn spec (detect_test_command's own -x) does —
# same reasoning tests/qa/test_qa_20_fallo_preexistente.py uses for its spec.
_PYTEST_SPEC = {
    "kind": "pytest",
    "argv": [sys.executable, "-m", "pytest", "-q", "--no-header",
             "-p", "no:cacheprovider", "--color=no"],
    "label": "pytest -q",
}


def _fake_export_tree(source_dir):
    def _export(workspace, sha, dest):
        shutil.copytree(str(source_dir), dest, dirs_exist_ok=True)
        return True
    return _export


def test_tests_kind_separates_new_from_preexisting_with_real_pytest(tmp_path, monkeypatch):
    current = tmp_path / "current"
    checkpoint = tmp_path / "checkpoint"
    for d in (current, checkpoint):
        (d / "tests").mkdir(parents=True)
        (d / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (checkpoint / "tests" / "test_thing.py").write_text(textwrap.dedent("""
        def test_already_broken():
            assert 1 == 2

        def test_stays_fine():
            assert 1 == 1
    """), encoding="utf-8")
    (current / "tests" / "test_thing.py").write_text(textwrap.dedent("""
        def test_already_broken():
            assert 1 == 2

        def test_stays_fine():
            assert 1 == 1

        def test_the_patch_just_broke():
            assert False
    """), encoding="utf-8")

    monkeypatch.setattr(wc, "export_tree", _fake_export_tree(checkpoint))
    monkeypatch.setattr(project_tests, "detect_test_command", lambda workspace, override=None: dict(_PYTEST_SPEC))

    result = ver.run_verifier("tests", str(current), checkpoint_sha="fake_sha",
                              changed=["tests/test_thing.py"])

    assert result["ran"] is True
    assert result["kind"] == "tests"
    assert any("test_the_patch_just_broke" in f for f in result["new_failures"])
    assert any("test_already_broken" in f for f in result["preexisting"])
    assert not any("test_the_patch_just_broke" in f for f in result["preexisting"])
    assert result["baseline"]["failed"] and result["after"]["failed"]


def test_tests_kind_reports_what_the_patch_fixed(tmp_path, monkeypatch):
    current = tmp_path / "current"
    checkpoint = tmp_path / "checkpoint"
    for d in (current, checkpoint):
        (d / "tests").mkdir(parents=True)
        (d / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (checkpoint / "tests" / "test_thing.py").write_text(textwrap.dedent("""
        def test_already_broken():
            assert 1 == 2

        def test_will_be_fixed():
            assert 1 == 2
    """), encoding="utf-8")
    (current / "tests" / "test_thing.py").write_text(textwrap.dedent("""
        def test_already_broken():
            assert 1 == 2

        def test_will_be_fixed():
            assert 1 == 1
    """), encoding="utf-8")

    monkeypatch.setattr(wc, "export_tree", _fake_export_tree(checkpoint))
    monkeypatch.setattr(project_tests, "detect_test_command", lambda workspace, override=None: dict(_PYTEST_SPEC))

    result = ver.run_verifier("tests", str(current), checkpoint_sha="fake_sha",
                              changed=["tests/test_thing.py"])

    assert any("test_will_be_fixed" in f for f in result["fixed"])
    assert not any("test_will_be_fixed" in f for f in result["new_failures"])


@pytest.mark.skipif(not shutil.which("make"), reason="make is not available in this environment")
def test_generic_kind_treats_an_unchanged_failure_as_preexisting(tmp_path, monkeypatch):
    current = tmp_path / "current"
    checkpoint = tmp_path / "checkpoint"
    current.mkdir()
    checkpoint.mkdir()
    (checkpoint / "Makefile").write_text("lint:\n\texit 1\n", encoding="utf-8")
    (current / "Makefile").write_text("lint:\n\texit 1\n", encoding="utf-8")

    monkeypatch.setattr(wc, "export_tree", _fake_export_tree(checkpoint))

    result = ver.run_verifier("lint", str(current), checkpoint_sha="fake_sha")

    assert result["ran"] is True
    assert result["preexisting"] and not result["new_failures"]


@pytest.mark.skipif(not shutil.which("make"), reason="make is not available in this environment")
def test_generic_kind_flags_a_failure_not_present_at_the_checkpoint(tmp_path, monkeypatch):
    current = tmp_path / "current"
    checkpoint = tmp_path / "checkpoint"
    current.mkdir()
    checkpoint.mkdir()
    (checkpoint / "Makefile").write_text("lint:\n\texit 0\n", encoding="utf-8")
    (current / "Makefile").write_text("lint:\n\texit 1\n", encoding="utf-8")

    monkeypatch.setattr(wc, "export_tree", _fake_export_tree(checkpoint))

    result = ver.run_verifier("lint", str(current), checkpoint_sha="fake_sha")

    assert result["new_failures"] and not result["preexisting"]


def test_unknown_kind_is_rejected_not_silently_skipped():
    result = ver.run_verifier("security", "/tmp", checkpoint_sha=None)
    assert result["ran"] is False
    assert "unknown verifier kind" in result["summary"]


def test_no_command_detected_is_inconclusive_never_a_false_pass(tmp_path):
    result = ver.run_verifier("build", str(tmp_path), checkpoint_sha=None)
    assert result["ran"] is False
    assert result["ok"] is None
    assert result["inconclusive"] is True
