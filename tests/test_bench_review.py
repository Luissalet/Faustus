"""BENCH-02 — `src/auto_review.py::review_gate` (a real review gate for a
ChangeSet, not the LLM diff-reading pass above it in the same module).

The one rule under test throughout: a `ChangeSet` with no exact diff, or with
no decided before/after test verdict, must never come out of `review_gate`
approved — no matter what else about it looks fine.
"""
from __future__ import annotations

import pytest

from src import auto_review as R
from src.contracts.changeset import ChangeSet

WORKSPACE = "/repo"
CHECKPOINT = "a" * 40


def _changeset(**overrides):
    raw = {
        "id": "cs-1", "intent": "implement", "workspace": WORKSPACE,
        "checkpoint": CHECKPOINT,
        "files": {"source": "checkpoint", "checkpoint": CHECKPOINT,
                  "modified": ["src/thing.py"]},
        "verification": {"mode": "tests", "ran": True, "ok": True,
                         "command": "pytest -q"},
    }
    raw.update(overrides)
    return ChangeSet.parse(raw)


# ── no diff at all ───────────────────────────────────────────────────────

def test_a_changeset_with_no_diff_is_never_approved():
    """The lot's own requirement, verbatim: "un ChangeSet sin diff no se
    puede aprobar como revisado"."""
    cs = _changeset(files={"source": "none"}, verification={"ran": True, "ok": True})
    result = R.review_gate(cs)
    assert result.approved is False
    assert any("no exact diff" in b for b in result.blocking)


def test_a_truncated_diff_does_not_count_as_exact_either():
    cs = _changeset(files={"source": "checkpoint", "checkpoint": CHECKPOINT,
                           "modified": ["src/thing.py"], "truncated": True})
    result = R.review_gate(cs)
    assert result.approved is False
    assert any("truncated" in b for b in result.blocking)


def test_a_git_derived_list_is_not_exact_even_with_files_named():
    """git status can miss an ignored file; only a checkpoint diff is exact
    enough for `FileChanges.exact`, and this gate inherits that rule rather
    than loosening it."""
    cs = _changeset(files={"source": "git", "modified": ["src/thing.py"]})
    result = R.review_gate(cs)
    assert result.approved is False
    assert any("files.source='git'" in b for b in result.blocking)


# ── no before/after test verdict ────────────────────────────────────────

def test_a_changeset_with_no_test_run_is_never_approved():
    cs = _changeset(verification={"mode": "none", "ran": False})
    result = R.review_gate(cs)
    assert result.approved is False
    assert any("no before/after test run" in b for b in result.blocking)


def test_an_inconclusive_test_run_blocks_approval_too():
    """`ok=None` is `run_verifier`'s "ran and decided nothing" — the contract
    keeps that distinct from both passed and failed on purpose, and the gate
    must not treat "ran" alone as enough."""
    cs = _changeset(verification={"mode": "tests", "ran": True, "ok": None,
                                  "inconclusive": True, "summary": "no test command detected"})
    result = R.review_gate(cs)
    assert result.approved is False
    assert any("did not reach a verdict" in b for b in result.blocking)


def test_failing_tests_do_not_block_approval_by_themselves():
    """A reviewed ChangeSet may honestly say "tests fail, here is why" — only
    the ABSENCE of a real verdict blocks, never a verdict of False."""
    cs = _changeset(verification={"mode": "tests", "ran": True, "ok": False,
                                  "command": "pytest -q", "summary": "2 failed",
                                  "failures": ["tests/test_thing.py::test_x"]})
    result = R.review_gate(cs)
    assert result.approved is True
    assert result.blocking == ()


# ── files touched outside declared scope ────────────────────────────────

def test_files_outside_the_declared_scope_are_named_and_block_approval():
    cs = _changeset(files={"source": "checkpoint", "checkpoint": CHECKPOINT,
                           "modified": ["src/thing.py", "src/other_module.py"]})
    result = R.review_gate(cs, declared_scope=["src/thing.py"])
    assert result.approved is False
    assert result.out_of_scope == ("src/other_module.py",)
    assert any("outside the declared scope" in b for b in result.blocking)
    assert any("src/other_module.py" in b for b in result.blocking)


def test_a_glob_scope_entry_covers_everything_under_it():
    cs = _changeset(files={"source": "checkpoint", "checkpoint": CHECKPOINT,
                           "modified": ["src/state_mirror/divergence.py"]})
    result = R.review_gate(cs, declared_scope=["src/state_mirror/*"])
    assert result.approved is True


def test_a_directory_scope_entry_covers_files_nested_under_it():
    cs = _changeset(files={"source": "checkpoint", "checkpoint": CHECKPOINT,
                           "modified": ["tests/eval/harness.py"]})
    result = R.review_gate(cs, declared_scope=["tests/eval"])
    assert result.approved is True


def test_no_declared_scope_means_no_scope_check_at_all():
    """Scope enforcement is opt-in: a caller that never declares one gets no
    out-of-scope findings, matching every review that predates BENCH-02."""
    cs = _changeset(files={"source": "checkpoint", "checkpoint": CHECKPOINT,
                           "modified": ["anything/at/all.py"]})
    result = R.review_gate(cs)
    assert result.approved is True
    assert result.out_of_scope == ()


def test_scope_is_not_checked_against_an_inexact_change_list():
    """Same reasoning as `ChangeSet.unsupported_claims`: a non-exact list
    cannot be used to accuse a turn of touching something out of scope --
    but it is still refused, on the exact-diff ground above."""
    cs = _changeset(files={"source": "git", "modified": ["src/anything.py"]})
    assert R.scope_violations(cs, ["src/thing.py"]) == ()
    result = R.review_gate(cs, declared_scope=["src/thing.py"])
    assert result.approved is False  # refused, but for the diff, not the scope
    assert result.out_of_scope == ()


# ── a fully-earned approval ─────────────────────────────────────────────

def test_a_changeset_with_a_real_diff_a_real_verdict_and_in_scope_is_approved():
    cs = _changeset()
    result = R.review_gate(cs, declared_scope=["src/thing.py"])
    assert result.approved is True
    assert result.blocking == ()
    assert result.out_of_scope == ()
    assert result.to_dict() == {"approved": True, "blocking": [], "out_of_scope": []}


# ── verification_from_run_verifier: reuses run_verifier's own vocabulary ──

def test_verification_from_run_verifier_maps_a_clean_pass():
    from src.contracts.changeset import Verification

    raw = {"kind": "tests", "ran": True, "ok": True, "inconclusive": False,
           "command": "pytest -q", "summary": "12 passed",
           "baseline": {"ran": True, "ok": True, "failed": [], "summary": "12 passed"},
           "after": {"ran": True, "ok": True, "failed": [], "summary": "12 passed"},
           "new_failures": [], "fixed": [], "preexisting": []}
    v = R.verification_from_run_verifier(raw)
    assert isinstance(v, Verification)
    assert v.ran is True and v.ok is True and v.failures == ()


def test_verification_from_run_verifier_carries_only_new_failures():
    """`new_failures` (broke because of THIS turn) becomes `.failures`;
    `preexisting` ones do not, and the run is flagged `pre_existing_only`
    when that is the whole story — a turn should not be blamed for a test
    that was already red before it started."""
    raw = {"kind": "tests", "ran": True, "ok": False, "inconclusive": False,
           "command": "pytest -q", "summary": "1 failed",
           "new_failures": [], "fixed": [], "preexisting": ["tests/test_old.py::test_flaky"]}
    v = R.verification_from_run_verifier(raw)
    assert v.pre_existing_only is True
    assert v.failures == ()


def test_verification_from_run_verifier_that_never_ran_stays_unverified():
    raw = {"kind": "tests", "ran": False, "ok": None, "inconclusive": True,
           "summary": "no tests command detected for this project"}
    v = R.verification_from_run_verifier(raw)
    assert v.ran is False and v.ok is None

    # And feeding that straight into a ChangeSet's verification blocks the
    # gate exactly like a hand-built Verification({"ran": False}) would —
    # proving the two paths a caller can build a ChangeSet's verification
    # from (by hand, or from a real run_verifier() call) are gated alike.
    cs = _changeset(verification=v.to_dict())
    assert R.review_gate(cs).approved is False


def test_a_full_run_verifier_result_produces_an_approvable_changeset():
    """End to end: the exact dict shape `src.verification.run_verifier`
    documents returning, fed through the converter and then the gate."""
    raw = {"kind": "tests", "ran": True, "ok": True, "inconclusive": False,
           "command": "pytest -q", "summary": "3 passed",
           "baseline": {"ran": True, "ok": True, "failed": [], "summary": "2 passed"},
           "after": {"ran": True, "ok": True, "failed": [], "summary": "3 passed"},
           "new_failures": [], "fixed": [], "preexisting": []}
    cs = _changeset(verification=R.verification_from_run_verifier(raw).to_dict())
    result = R.review_gate(cs)
    assert result.approved is True
