"""
tests/test_changesets.py — "I fixed it", with the evidence attached.

The masterplan's rule for Phase 5 is one sentence: *no claim of a fix ends
without a diff and evidence appropriate to the mode.* So the tests that matter
are the ones where a report tries to be more certain than its evidence allows.

Three of them carry the file:

* a claim about a file that did not change is **named**, and the refusal says
  what it was instead of what it claimed;
* the same check is deliberately SILENT when the evidence is not exact — a
  truncated list cannot contradict anything, and reporting it as a false claim
  would be the same overreach pointing the other way;
* `ok` is three-valued and `None` means NOT VERIFIED. A verdict on a run that
  did not happen is refused at parse.

There is no fifth verdict vocabulary here. `judge()` hands the change set to
`prove`, which already speaks proved/partial/unproved/contradicted with named
doubts, and adding another word for "did it work" would be the problem rather
than the solution.
"""
from __future__ import annotations

import pytest

from src import changesets
from src.contracts import ChangeSet, ContractError


def base(**over):
    body = {
        "id": "chg_test", "intent": "implement", "workspace": "D:/proj",
        "title": "Fix the rate limiter",
        "checkpoint": "a" * 40,
        "files": {"source": "checkpoint", "checkpoint": "a" * 40,
                  "modified": ["src/limiter.py"], "added": [], "deleted": []},
        "claims": [{"path": "src/limiter.py", "kind": "modified"}],
        "verification": {"mode": "tests", "ran": True, "ok": True,
                         "command": "pytest -q tests/test_limiter.py",
                         "summary": "12 passed"},
    }
    body.update(over)
    return ChangeSet.parse(body)


# ── evidence that does not support the claim ──────────────────────────────

def test_a_claim_about_a_file_that_did_not_change_is_named(): 
    """The most common false report an agent produces, and the one that is
    indistinguishable from a real fix in a summary."""
    changeset = base(claims=[{"path": "src/limiter.py", "kind": "modified"},
                             {"path": "src/cache.py", "kind": "modified"}])
    problems = {p["path"]: p for p in changeset.unsupported_claims()}
    assert set(problems) == {"src/cache.py"}
    assert "nothing changed at that path" in problems["src/cache.py"]["reason"]


def test_a_claim_of_the_wrong_kind_says_what_it_actually_was():
    changeset = base(claims=[{"path": "src/limiter.py", "kind": "created"}])
    problem = changeset.unsupported_claims()[0]
    assert problem["claimed"] == "created"
    assert "it was modified, not created" in problem["reason"]


def test_a_claim_that_a_file_was_left_alone_is_checked_too():
    changeset = base(claims=[{"path": "src/limiter.py", "kind": "untouched"}])
    assert changeset.unsupported_claims()[0]["reason"] == "it changed"


def test_a_bare_filename_matches_its_path_but_not_a_lookalike():
    """A model says `limiter.py` for `src/limiter.py` constantly, and calling
    that a false claim would teach everybody to ignore the check. But
    `cart.py` must not match `shopping_cart.py`."""
    assert base(claims=[{"path": "limiter.py"}]).unsupported_claims() == ()

    lookalike = base(
        files={"source": "checkpoint", "checkpoint": "a" * 40,
               "modified": ["src/shopping_cart.py"]},
        claims=[{"path": "cart.py"}])
    assert lookalike.unsupported_claims(), "a suffix matched across a word boundary"


def test_evidence_that_is_not_exact_contradicts_nothing():
    """A truncated or mtime-derived list cannot be used to call a claim false.
    Being silent here is the same discipline as naming the claim above."""
    for files in ({"source": "checkpoint", "checkpoint": "a" * 40,
                   "modified": ["src/limiter.py"], "truncated": True},
                  {"source": "mtime", "modified": ["src/limiter.py"]},
                  {"source": "none"}):
        changeset = base(files=files, checkpoint="")
        assert changeset.unsupported_claims() == (), files["source"]


def test_a_file_that_changed_and_nobody_mentioned_is_pointed_out():
    changeset = base(files={"source": "checkpoint", "checkpoint": "a" * 40,
                            "modified": ["src/limiter.py", "src/secrets.py"]})
    assert changeset.unclaimed_changes() == ("src/secrets.py",)


# ── a verdict from a run that did not happen ──────────────────────────────

def test_a_result_without_a_run_is_refused_at_parse():
    with pytest.raises(ContractError) as err:
        base(verification={"mode": "tests", "ran": False, "ok": True})
    assert err.value.path == "changeset.verification.ok"
    assert "did not happen" in err.value.message


def test_not_verified_is_not_the_same_as_passed():
    changeset = base(verification={"mode": "none", "ran": False})
    assert changeset.verification.ok is None
    assert changeset.verification.passed is False
    assert changeset.verification.failed is False, "not-run was read as a failure"
    kinds = {g["kind"] for g in changeset.evidence_gaps()}
    assert "no_verification_runner" in kinds


def test_ok_has_to_be_a_boolean_or_absent():
    with pytest.raises(ContractError) as err:
        base(verification={"mode": "tests", "ran": True, "ok": "yes"})
    assert "NOT VERIFIED, not passed" in err.value.message


def test_failures_that_were_already_there_are_not_this_turn_s():
    changeset = base(verification={"mode": "tests", "ran": True, "ok": False,
                                   "pre_existing_only": True,
                                   "summary": "3 failed, all before this turn"})
    assert changeset.verification.failed is False
    assert "pre_existing_failures" in {g["kind"] for g in changeset.evidence_gaps()}


# ── the mode is a promise ─────────────────────────────────────────────────

@pytest.mark.parametrize("intent", ["explore", "plan", "review"])
def test_an_intent_that_promised_to_look_may_not_have_written(intent):
    """Not a stricter outcome — a different one than the one announced. The
    point of naming the intent is that somebody agreed to it."""
    with pytest.raises(ContractError) as err:
        base(intent=intent, claims=[])
    assert err.value.path == "changeset.files"
    assert f"'{intent}' says the workspace will be left alone" in err.value.message
    assert "src/limiter.py" in err.value.message


def test_a_review_that_changed_nothing_is_perfectly_fine():
    changeset = base(intent="review", files={"source": "checkpoint",
                                             "checkpoint": "a" * 40},
                     claims=[], verification={"mode": "none", "ran": False})
    assert changeset.files.paths == ()
    # …and it is NOT asked for tests it never promised
    assert {g["kind"] for g in changeset.evidence_gaps()} == set()


def test_an_implement_that_changed_nothing_is_a_gap():
    changeset = base(files={"source": "checkpoint", "checkpoint": "a" * 40},
                     claims=[])
    assert "no_changes" in {g["kind"] for g in changeset.evidence_gaps()}


def test_an_unknown_intent_lists_the_ones_that_exist():
    with pytest.raises(ContractError) as err:
        base(intent="vibes")
    assert "explore" in str(err.value) and "implement" in str(err.value)


# ── the record has to be about this turn ──────────────────────────────────

def test_a_diff_against_a_different_starting_point_is_not_evidence():
    with pytest.raises(ContractError) as err:
        base(checkpoint="b" * 40)
    assert "not evidence about this turn" in err.value.message


def test_a_checkpoint_source_has_to_name_the_checkpoint():
    with pytest.raises(ContractError) as err:
        base(files={"source": "checkpoint", "modified": ["a.py"]}, checkpoint="")
    assert "has to name it" in err.value.message


def test_a_command_kept_as_one_string_is_refused():
    """Same rule as ExecutionSpec: a command recorded as a string cannot be
    replayed without guessing where the quoting was."""
    with pytest.raises(ContractError) as err:
        base(commands=[{"argv": "pytest -q tests/"}])
    assert "not one string" in err.value.message

    ok = base(commands=[{"argv": ["pytest", "-q", "tests/"], "exit_code": 0}])
    assert ok.commands[0].argv == ("pytest", "-q", "tests/")


def test_the_fingerprint_is_of_the_work_not_of_the_record():
    """Two reports of the same turn have the same fingerprint, which is what
    makes "you already told me this" answerable."""
    first = base(id="chg_one", created_at="2026-09-04T10:00:00Z")
    second = base(id="chg_two", created_at="2026-09-04T11:30:00Z")
    assert first.fingerprint() == second.fingerprint()
    assert base(title="Something else").fingerprint() == first.fingerprint()
    assert base(claims=[]).fingerprint() != first.fingerprint()


# ── the verdict is delegated ──────────────────────────────────────────────

def test_the_verdict_comes_from_prove_rather_than_from_a_new_vocabulary():
    proof = changesets.judge(base())
    assert proof["verdict"] in ("proved", "partial", "unproved", "contradicted")
    assert 0.0 <= proof["confidence"] <= 1.0
    assert "identity" in proof


def test_a_change_set_with_no_verification_cannot_come_out_proved():
    proof = changesets.judge(base(verification={"mode": "none", "ran": False}))
    assert proof["verdict"] != "proved"
    assert proof["confidence"] < 1.0
    assert proof["uncertainty"], "less than certain, and unable to say why"


def test_the_contract_s_own_gaps_reach_the_proof():
    """One report must not sound surer in one field than in another. An
    `implement` that changed nothing is a doubt the contract can see and
    `prove` cannot, so it is folded in."""
    empty = base(files={"source": "checkpoint", "checkpoint": "a" * 40}, claims=[])
    kinds = {u["kind"] for u in changesets.judge(empty)["uncertainty"]}
    assert "no_changes" in kinds


def test_a_confident_proof_still_carries_no_contradiction():
    proof = changesets.judge(base())
    assert not any(u["kind"] == "claim_not_on_disk"
                   for u in proof.get("uncertainty") or ())


# ── building one from what Faustus already records ────────────────────────

def test_a_turn_ledger_summary_becomes_a_change_set():
    summary = {
        "mutations": ["src/limiter.py"],
        "checkpoint": "c" * 40,
        "tests": {"ran": True, "ok": True, "command": "pytest -q",
                  "summary": "12 passed"},
        "review": {"verdict": "ok", "findings": []},
        "language": "python", "tools_run": ["edit_file"],   # ignored, not an error
    }
    changeset = changesets.from_turn(summary, workspace="D:/proj",
                                     claims=[{"path": "src/limiter.py"}])
    assert changeset.intent == "implement"
    assert changeset.files.modified == ("src/limiter.py",)
    assert changeset.files.exact is True
    assert changeset.verification.passed is True
    assert changeset.unsupported_claims() == ()
    assert changeset.review["verdict"] == "ok"


def test_a_turn_that_ran_no_tests_does_not_get_an_ok_out_of_nowhere():
    changeset = changesets.from_turn(
        {"mutations": ["a.py"], "checkpoint": "c" * 40, "tests": {"ran": False}},
        workspace="D:/proj")
    assert changeset.verification.ok is None
    assert changeset.verification.ran is False


def test_a_dispatch_job_becomes_a_change_set_with_its_claims_intact():
    """Dispatch already does the hard half: it overwrites the workers' claimed
    file list with what Faustus SAW, and keeps the difference in
    `claimed_only`. That difference is what becomes a claim here."""
    compact = {
        "id": "job1", "title": "Workers · fix the limiter",
        "changes": {"source": "checkpoint", "checkpoint": "d" * 40,
                    "modified": ["src/limiter.py"], "added": [], "deleted": []},
        "claimed_only": ["src/cache.py"],
        "verification": {"mode": "tests", "ran": True, "ok": True,
                         "summary": "9 passed"},
    }
    changeset = changesets.from_dispatch(compact, workspace="D:/proj")
    assert changeset.run_id == "job1"
    assert changeset.checkpoint == "d" * 40
    problems = changeset.unsupported_claims()
    assert [p["path"] for p in problems] == ["src/cache.py"]


# ── reading one ───────────────────────────────────────────────────────────

def test_the_rendering_leads_with_the_verdict_and_then_the_doubts():
    """Somebody scanning this is asking "can I trust the sentence above", and
    the answer to that is the doubts, not the file list."""
    text = changesets.render(base())
    lines = text.splitlines()
    assert lines[0].startswith("chg_test · implement")
    assert "tests: passed" in text
    assert "12 passed" in text
    assert "1 file(s) changed [checkpoint]" in text
    assert "~ src/limiter.py" in text
    assert text.index("tests: passed") < text.index("src/limiter.py")


def test_the_rendering_names_a_claim_it_could_not_see():
    text = changesets.render(base(claims=[{"path": "src/ghost.py"}]))
    assert "CLAIMED BUT NOT SEEN: src/ghost.py" in text


def test_the_rendering_says_when_nothing_was_run():
    text = changesets.render(base(verification={"mode": "none", "ran": False}))
    assert "nothing was run to check this" in text


def test_the_diff_is_fetched_when_asked_and_a_missing_one_says_so():
    """A ChangeSet holds the sha, not the diff text — reading one costs
    nothing. And a missing diff and an empty diff are opposite facts."""
    out = changesets.diff_of(base(checkpoint="", files={"source": "none"}))
    assert out["ok"] is False and out["reason"] == "no_checkpoint"

    out = changesets.diff_of(base(workspace=""))
    assert out["ok"] is False and out["reason"] == "no_workspace"


def test_a_checkpoint_this_machine_never_had_is_not_an_empty_diff(tmp_path):
    """Found by running it for real. Every read in `workspace_checkpoints`
    answers a sha it has never heard of with the same empty result it gives
    for "nothing changed" — so a checkpoint from another data directory would
    have been reported as "the work did nothing", which is the exact false
    reassurance this whole contract exists to prevent."""
    out = changesets.diff_of(base(workspace=str(tmp_path),
                                  checkpoint="f" * 40,
                                  files={"source": "checkpoint",
                                         "checkpoint": "f" * 40,
                                         "modified": ["a.py"]}))
    assert out["ok"] is False
    assert out["reason"] == "unknown_checkpoint"
    assert "empty diff would be a different claim" in out["detail"]


def test_against_a_real_checkpoint_the_diff_is_the_real_diff(tmp_path):
    """No fixtures: a real folder, a real shadow git repo, a real edit."""
    from src import workspace_checkpoints

    if not workspace_checkpoints.git_available():
        pytest.skip("no git on this machine")

    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "limiter.py").write_text("RATE = 10\n", encoding="utf-8")
    (tmp_path / "proj" / "untouched.py").write_text("x = 1\n", encoding="utf-8")

    made = workspace_checkpoints.checkpoint(workspace, label="before")
    if not made:
        pytest.skip("checkpoints are switched off on this machine")
    sha = made["sha"]
    assert workspace_checkpoints.has_checkpoint(workspace, sha)

    (tmp_path / "proj" / "limiter.py").write_text("RATE = 100\n", encoding="utf-8")
    (tmp_path / "proj" / "secrets.py").write_text("TOKEN = 'oops'\n", encoding="utf-8")

    seen = workspace_checkpoints.changed_since(workspace, sha)
    changes = {"source": "checkpoint", "checkpoint": sha,
               "added": [r["path"] for r in seen if r["status"] == "A"],
               "modified": [r["path"] for r in seen if r["status"] == "M"],
               "deleted": [r["path"] for r in seen if r["status"] == "D"]}

    changeset = changesets.build(
        intent="fix", workspace=workspace, checkpoint=sha, changes=changes,
        verification={"mode": "tests", "ran": True, "ok": True,
                      "summary": "3 passed"},
        claims=[{"path": "limiter.py", "kind": "modified"},
                {"path": "untouched.py", "kind": "untouched"},
                {"path": "cache.py", "kind": "modified"}])

    # The claim check, run against what git actually saw.
    problems = {p["path"]: p["reason"] for p in changeset.unsupported_claims()}
    assert set(problems) == {"cache.py"}, problems
    assert changeset.unclaimed_changes() == ("secrets.py",)

    out = changesets.diff_of(changeset)
    assert out["ok"] is True and out["empty"] is False
    assert "RATE = 100" in out["diff"] and "RATE = 10" in out["diff"]
    assert "secrets.py" in out["diff"]
    assert "untouched.py" not in out["diff"]

    # And one file's worth of it, since that is what a reviewer asks for.
    one = changesets.diff_of(changeset, path="limiter.py")
    assert "RATE = 100" in one["diff"] and "secrets.py" not in one["diff"]
