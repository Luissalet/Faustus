"""EDIT-04 · Cobertura real del checkpoint (Lote 19, closes QA-18's actual gap).

`workspace_checkpoints.checkpoint_coverage(workspace, sha)` reports what a
`restore()` to that checkpoint would and would not put back:
`{covered, uncovered: [{path, reason}], external_effects}`. `restore()` now
returns that report under `coverage` instead of only `{restored, deleted,
failed, unchanged}` — so a caller can say what stayed out of scope (vendored
dirs, binaries/oversized files the shadow repo never snapshots, irreversible
external effects the caller names) instead of announcing a plain "reverted"
that a restore never actually guaranteed (docs/spec/v2/acceptance_scenarios.json
QA-18).

tests/qa/test_qa_18_rollback_fuera_de_alcance.py stays exactly as it is
(xfail(strict=True), probing `status()`, an ajeno file outside this lote's
PROPIOS — see the final report's "Cambios necesarios en ficheros ajenos").
This file is the real regression test for EDIT-04 against the functions this
lote actually owns.
"""
import os

import pytest

from src import workspace_checkpoints as wc

pytestmark = pytest.mark.skipif(not wc.git_available(), reason="git not available")


def test_checkpoint_coverage_reports_covered_files_that_changed_since_the_checkpoint(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    cp = wc.checkpoint(str(tmp_path), "initial")
    assert cp is not None
    (tmp_path / "a.txt").write_text("hello world\n")
    cov = wc.checkpoint_coverage(str(tmp_path), cp["sha"])
    assert cov["covered"] == ["a.txt"]
    assert cov["uncovered"] == []
    assert cov["external_effects"] == []


def test_checkpoint_coverage_names_vendored_dirs_as_uncovered(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    os.makedirs(tmp_path / "node_modules")
    (tmp_path / "node_modules" / "x.js").write_text("x")
    cp = wc.checkpoint(str(tmp_path), "initial")
    cov = wc.checkpoint_coverage(str(tmp_path), cp["sha"])
    reasons = {u["path"]: u["reason"] for u in cov["uncovered"]}
    assert reasons.get("node_modules/") == "excluded_dir_never_snapshotted"


def test_checkpoint_coverage_carries_caller_supplied_external_effects(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    cp = wc.checkpoint(str(tmp_path), "initial")
    effects = [{"kind": "email_sent", "detail": "invoice reminder to client@example.com"}]
    cov = wc.checkpoint_coverage(str(tmp_path), cp["sha"], external_effects=effects)
    assert cov["external_effects"] == effects


def test_checkpoint_coverage_of_an_unknown_sha_says_so_instead_of_claiming_nothing_changed():
    cov = wc.checkpoint_coverage("/nonexistent/workspace/path", "deadbeef" * 5)
    assert cov["uncovered"] and cov["uncovered"][0]["reason"] in (
        "unknown_checkpoint", "checkpoints_unavailable")
    assert cov["covered"] == []


def test_restore_report_includes_coverage_instead_of_announcing_a_bare_reversion(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    os.makedirs(tmp_path / "node_modules")
    (tmp_path / "node_modules" / "x.js").write_text("x")
    cp = wc.checkpoint(str(tmp_path), "initial")
    (tmp_path / "a.txt").write_text("changed\n")
    result = wc.restore(str(tmp_path), cp["sha"])
    # The keys restore() has always returned are untouched (compatibility).
    assert result["restored"] == ["a.txt"]
    # And the new field says what this restore could not touch.
    assert "coverage" in result
    reasons = {u["path"]: u["reason"] for u in result["coverage"]["uncovered"]}
    assert reasons.get("node_modules/") == "excluded_dir_never_snapshotted"


# ── revert-proof: before EDIT-04, restore()'s result had no `coverage` key
# at all, so a caller reading it could not tell "nothing else changed" from
# "this restore has no idea about node_modules/". ──────────────────────────
def test_old_restore_result_shape_had_no_coverage_field(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    cp = wc.checkpoint(str(tmp_path), "initial")
    (tmp_path / "a.txt").write_text("changed\n")
    legacy_keys = {"restored", "deleted", "failed", "unchanged", "sha"}
    result = wc.restore(str(tmp_path), cp["sha"])
    assert legacy_keys.issubset(result.keys())   # nothing old was removed (compat)
    assert "coverage" in result and "coverage" not in legacy_keys  # the new field is genuinely new
