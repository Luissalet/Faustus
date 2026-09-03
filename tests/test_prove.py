"""The `prove` step (src/prove.py) — "a mutation is not the completion of the
objective".

The transport's ack is not evidence and neither is a worker's word. What is
checked here: each of the four verdicts from hand-built evidence, `unproved`
as a value of its own (no runner and nothing observable changed — NOT a
failure and NOT an error), a named uncertainty entry for every reason the
confidence dropped, the invariant that the list is never empty below 1.0, an
identity that survives the transport reordering or paginating the change list
but cannot be forged by re-splitting a field (the length-prefix rule), and the
off state of `agent_dispatch_prove` reproducing yesterday's dispatch payload
and verdict line byte for byte.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import src.settings as settings_mod
from src import dispatch, prove
from tests.test_dispatch_reliability import _no_checkpoints, _worker_report, fake_tool  # noqa: F401

EXACT = {"source": "checkpoint", "count": 2, "added": ["src/cart.py"], "modified": ["tests/test_cart.py"],
         "deleted": [], "truncated": False, "checkpoint": "9f1c2b3d4e5f"}
NOTHING = {"source": "checkpoint", "count": 0, "added": [], "modified": [], "deleted": [], "truncated": False,
           "checkpoint": "9f1c2b3d4e5f"}
PASSED = {"mode": "auto", "ran": True, "ok": True, "inconclusive": False, "command": "pytest -q",
          "summary": "7 passed"}
FAILED = {"mode": "auto", "ran": True, "ok": False, "inconclusive": False, "command": "pytest -q",
          "summary": "1 failed", "failures": ["tests/test_cart.py::test_discount"]}
NO_RUNNER = {"mode": "auto", "ran": False, "ok": None,
             "summary": "no test runner detected in the workspace"}


def _kinds(p):
    return [u["kind"] for u in p["uncertainty"]]


# ── the four verdicts ───────────────────────────────────────────────────────

def test_proved_is_a_passing_verification_and_every_claim_on_disk():
    p = prove.prove(EXACT, PASSED, ["src/cart.py", "tests/test_cart.py"])
    assert p["verdict"] == "proved"
    assert p["confidence"] == 1.0 and p["uncertainty"] == []
    assert p["schema_version"] == 1 and len(p["identity"]) == 64
    # the observations say what it was proved FROM, not that a command ran
    kinds = [o["kind"] for o in p["observations"]]
    assert "changes" in kinds and "verification" in kinds


def test_a_worker_naming_a_path_the_diff_names_with_its_folder_still_counts():
    """The worker says `cart.py`, the checkpoint diff says `src/cart.py` —
    the same comparison src/dispatch.py already uses for `claimed_only`."""
    p = prove.prove(EXACT, PASSED, ["cart.py", "test_cart.py"])
    assert p["verdict"] == "proved" and p["uncertainty"] == []


def test_partial_when_the_verification_could_not_decide():
    v = dict(PASSED, ok=None, inconclusive=True, summary="pytest exited 2 without a report")
    p = prove.prove(EXACT, v, ["src/cart.py"])
    assert p["verdict"] == "partial"
    assert "verification_inconclusive" in _kinds(p)
    assert p["confidence"] < 1.0


def test_partial_when_a_claim_has_no_change_behind_it_and_the_evidence_is_not_exact():
    mtime = dict(EXACT, source="mtime")
    mtime.pop("checkpoint")
    p = prove.prove(mtime, PASSED, ["src/cart.py", "src/ghost.py"])
    assert p["verdict"] == "partial"
    assert "claims_unaccounted" in _kinds(p) and "mtime_only" in _kinds(p)
    detail = [u for u in p["uncertainty"] if u["kind"] == "claims_unaccounted"][0]["detail"]
    assert "src/ghost.py" in detail


def test_unproved_is_no_runner_and_nothing_observable_and_it_is_not_a_failure():
    p = prove.prove(NOTHING, NO_RUNNER, [])
    assert p["verdict"] == "unproved"
    # distinct from partial, from contradicted, and from an error
    assert p["verdict"] not in ("partial", "contradicted", "error")
    assert "error" not in p and p["confidence"] > 0
    assert set(_kinds(p)) >= {"no_verification_runner", "no_observable_change"}


def test_unproved_when_a_worker_claims_work_nothing_can_see():
    """No runner, no checkpoint at all: the work may have happened and nothing
    here can show it. That is not a failure and not a contradiction."""
    p = prove.prove(None, NO_RUNNER, ["src/cart.py"])
    assert p["verdict"] == "unproved"
    assert set(_kinds(p)) >= {"no_verification_runner", "no_checkpoint", "claims_unaccounted"}


def test_contradicted_when_the_verification_failed():
    p = prove.prove(EXACT, FAILED, ["src/cart.py", "tests/test_cart.py"])
    assert p["verdict"] == "contradicted"
    assert "verification_failed" in _kinds(p)
    assert p["confidence"] <= 0.05


def test_a_failure_that_predates_the_job_is_not_a_contradiction():
    v = dict(FAILED, pre_existing_only=True)
    p = prove.prove(EXACT, v, ["src/cart.py"])
    assert p["verdict"] == "partial" and "pre_existing_failures" in _kinds(p)


def test_contradicted_when_an_exact_checkpoint_does_not_contain_a_claimed_path():
    p = prove.prove(EXACT, PASSED, ["src/cart.py", "src/ghost.py"])
    assert p["verdict"] == "contradicted"
    assert "claim_not_on_disk" in _kinds(p)
    assert "src/ghost.py" in [u for u in p["uncertainty"] if u["kind"] == "claim_not_on_disk"][0]["detail"]


def test_a_truncated_change_list_downgrades_a_missing_claim_instead_of_condemning_it():
    p = prove.prove(dict(EXACT, truncated=True), PASSED, ["src/cart.py", "src/ghost.py"])
    assert p["verdict"] == "partial"
    assert "truncated_changes" in _kinds(p) and "claims_unaccounted" in _kinds(p)
    assert "claim_not_on_disk" not in _kinds(p)


def test_contradicted_when_the_disk_says_the_other_kind():
    p = prove.prove(EXACT, PASSED, [{"path": "src/cart.py", "kind": "deleted"}])
    assert p["verdict"] == "contradicted"
    assert "claim_kind_mismatch" in _kinds(p)
    assert "claimed deleted, observed added" in \
        [u for u in p["uncertainty"] if u["kind"] == "claim_kind_mismatch"][0]["detail"]


# ── every named cause is a named entry ──────────────────────────────────────

@pytest.mark.parametrize("evidence, verification, claims, kind", [
    (EXACT, NO_RUNNER, [], "no_verification_runner"),
    (None, PASSED, [], "no_checkpoint"),
    (dict(EXACT, truncated=True), PASSED, [], "truncated_changes"),
    (dict(EXACT, source="mtime"), PASSED, [], "mtime_only"),
    (NOTHING, PASSED, [], "no_observable_change"),
    (dict(EXACT, source="mtime"), PASSED, ["ghost.py"], "claims_unaccounted"),
    (EXACT, PASSED, ["ghost.py"], "claim_not_on_disk"),
    (EXACT, FAILED, [], "verification_failed"),
    (EXACT, dict(PASSED, ok=None, inconclusive=True), [], "verification_inconclusive"),
    (EXACT, PASSED, {"paths": [], "workers": [{"name": "w1", "status": "stopped"}]}, "worker_cancelled"),
    (EXACT, PASSED, {"paths": [], "workers": [{"name": "w1", "status": "done", "outcome": "cancelled"}]},
     "worker_cancelled"),
    (EXACT, PASSED, {"paths": [], "workers": [{"name": "w1", "status": "timeout"}]}, "worker_unfinished"),
])
def test_each_named_cause_gets_its_own_uncertainty_entry(evidence, verification, claims, kind):
    p = prove.prove(evidence, verification, claims)
    assert kind in _kinds(p), f"{kind} missing from {_kinds(p)}"
    entry = [u for u in p["uncertainty"] if u["kind"] == kind][0]
    assert entry["detail"].strip(), "an uncertainty without a detail explains nothing"
    assert p["confidence"] < 1.0


def test_a_cancelled_worker_holds_the_proof_back_without_calling_it_a_failure():
    claims = {"paths": ["src/cart.py"], "workers": [{"name": "w1", "status": "done"},
                                                    {"name": "w2", "status": "stopped"}]}
    p = prove.prove(EXACT, PASSED, claims)
    assert p["verdict"] == "partial"                      # not proved, not contradicted
    entry = [u for u in p["uncertainty"] if u["kind"] == "worker_cancelled"][0]
    assert "not a failure" in entry["detail"] and "w2" in entry["detail"]


def test_the_uncertainty_list_is_never_empty_below_full_confidence():
    cases = [
        (EXACT, PASSED, ["src/cart.py", "tests/test_cart.py"]),
        (EXACT, PASSED, ["ghost.py"]),
        (EXACT, FAILED, []),
        (NOTHING, NO_RUNNER, []),
        (None, None, None),
        ({}, {}, []),
        (dict(EXACT, source="mtime"), dict(PASSED, ok=None, inconclusive=True), ["x.py"]),
        (EXACT, PASSED, {"paths": [], "workers": [{"name": "w", "status": "stalled"}]}),
    ]
    for evidence, verification, claims in cases:
        p = prove.prove(evidence, verification, claims)
        assert 0.0 <= p["confidence"] <= 1.0
        if p["confidence"] < 1.0:
            assert p["uncertainty"], f"confidence {p['confidence']} with nothing to explain it: {p}"
        else:
            assert p["uncertainty"] == []
        # the heaviest reason comes first, so a one-line verdict says the worst
        weights = [prove.PENALTY.get(u["kind"], 0.1) for u in p["uncertainty"]]
        assert weights == sorted(weights, reverse=True)


def test_the_verdict_line_names_the_verdict_and_the_top_uncertainty():
    p = prove.prove(NOTHING, NO_RUNNER, [])
    line = prove.line(p)
    assert line.startswith("proof unproved (")
    assert "no_verification_runner" in line
    assert prove.line(None) == "" and prove.line({}) == ""
    assert prove.top_uncertainty(p)["kind"] == "no_verification_runner"
    assert prove.top_uncertainty(prove.prove(EXACT, PASSED, [])) is None


# ── identity ────────────────────────────────────────────────────────────────

def test_identity_is_the_same_for_the_same_inputs_byte_for_byte():
    a = prove.prove(EXACT, PASSED, ["src/cart.py"], now=1.0)
    b = prove.prove(json.loads(json.dumps(EXACT)), json.loads(json.dumps(PASSED)), ["src/cart.py"], now=999.0)
    assert a["identity"] == b["identity"]                  # the clock is not part of it


def test_identity_survives_the_transport_reordering_or_paginating_the_change_list():
    paged = {
        "source": "checkpoint", "count": 2, "checkpoint": "9f1c2b3d4e5f", "truncated": False,
        # page 2 first, page 1 second, with the boundary row repeated — what a
        # paginating transport does to a list
        "added": ["src/cart.py", "src/cart.py"],
        "modified": ["tests/test_cart.py", "tests/test_cart.py"],
        "deleted": [],
    }
    assert prove.prove(paged, PASSED, ["tests/test_cart.py", "src/cart.py"])["identity"] == \
        prove.prove(EXACT, PASSED, ["src/cart.py", "tests/test_cart.py"])["identity"]


def test_identity_length_prefixes_every_variable_length_field():
    """Without a length in front of each element, ["ab", "c"] and ["a", "bc"]
    concatenate to the same bytes and hash the same. They must not."""
    assert prove.identity_of([("claims", ["ab", "c"])]) != prove.identity_of([("claims", ["a", "bc"])])
    assert prove.identity_of([("a", "x"), ("b", "yz")]) != prove.identity_of([("a", "xy"), ("b", "z")])
    assert prove.identity_of([("ab", "c")]) != prove.identity_of([("a", "bc")])
    # and the same split through the real entry point
    one = prove.prove({"source": "checkpoint", "added": ["ab", "c"], "modified": [], "deleted": []}, PASSED, [])
    two = prove.prove({"source": "checkpoint", "added": ["a", "bc"], "modified": [], "deleted": []}, PASSED, [])
    assert one["identity"] != two["identity"]


def test_identity_changes_when_the_evidence_really_changes():
    base = prove.prove(EXACT, PASSED, ["src/cart.py"])["identity"]
    assert prove.prove(dict(EXACT, checkpoint="0000deadbeef"), PASSED, ["src/cart.py"])["identity"] != base
    assert prove.prove(EXACT, FAILED, ["src/cart.py"])["identity"] != base
    assert prove.prove(EXACT, PASSED, ["src/cart.py", "extra.py"])["identity"] != base
    assert prove.prove(dict(EXACT, added=["src/cart.py", "new.py"]), PASSED, ["src/cart.py"])["identity"] != base
    # a path moved between kinds is a different evidence, not the same one
    swapped = dict(EXACT, added=["tests/test_cart.py"], modified=["src/cart.py"])
    assert prove.prove(swapped, PASSED, ["src/cart.py"])["identity"] != base


# ── total ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("evidence, verification, claims", [
    (object(), object(), object()),
    ("not a dict", 3, "abc"),
    ({"added": 5, "modified": None, "deleted": "x"}, {"ran": "yes", "ok": "maybe"}, [None, 3, {"path": None}]),
    ([1, 2], [], {"paths": "cart.py", "workers": "w1"}),
    ({"count": "many", "truncated": "sure"}, {"failures": [None]}, {"workers": [None, 7]}),
])
def test_prove_never_raises_whatever_it_is_handed(evidence, verification, claims):
    p = prove.prove(evidence, verification, claims)
    assert p["verdict"] in prove.VERDICTS
    assert 0.0 <= p["confidence"] <= 1.0
    assert isinstance(p["uncertainty"], list) and isinstance(p["observations"], list)
    assert len(p["identity"]) == 64 and p["schema_version"] == 1


def test_the_clock_is_injectable_and_never_part_of_the_identity():
    p = prove.prove(EXACT, PASSED, [], now=123.5)
    assert p["at"] == 123.5
    assert p["identity"] == prove.prove(EXACT, PASSED, [], now=0.0)["identity"]


def test_an_internal_failure_answers_unproved_not_proved(monkeypatch):
    monkeypatch.setattr(prove, "_prove", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    p = prove.prove(EXACT, PASSED, [], now=7.0)
    assert p["verdict"] == "unproved" and p["confidence"] == 0.0
    assert _kinds(p) == ["internal_error"] and "boom" in p["uncertainty"][0]["detail"]


# ── wired into a dispatched job ─────────────────────────────────────────────

async def _run_job(fake_tool, monkeypatch, *, verify="none"):
    _no_checkpoints(monkeypatch)
    ws = Path(fake_tool["ws"])
    (ws / "old.py").write_text("x = 1\n")
    (ws / "keep.py").write_text("k = 1\n")
    os.utime(ws / "old.py", (1_600_000_000, 1_600_000_000))

    def hook(args):
        (ws / "old.py").write_text("x = 2\n")
        (ws / "via_bash.txt").write_text("made by a shell command\n")
        (ws / "keep.py").unlink()

    fake_tool["before"] = hook
    fake_tool["result"] = {"output": "r", "exit_code": 0, "lock_conflicts": [], "dropped_tasks": 0,
                           "subagents": [_worker_report(mutations=["old.py", "ghost.py"],
                                                        final_text="All 7 tests pass.")]}
    job = await dispatch.start("luis", {"tasks": ["add apply_tax; pytest must pass"], "workspace": str(ws),
                                        "verify": verify})
    await dispatch.wait(job, 5)
    return job


async def test_a_finished_job_carries_the_proof_and_says_it_on_the_verdict_line(fake_tool, monkeypatch):
    job = await _run_job(fake_tool, monkeypatch)
    proof = dispatch.compact(job)["result"]["proof"]
    # the worker said "All 7 tests pass"; nothing ran that could show it
    assert proof["verdict"] == "partial" and proof["confidence"] < 1.0
    assert "no_verification_runner" in [u["kind"] for u in proof["uncertainty"]]
    assert "ghost.py" in [u["detail"] for u in proof["uncertainty"] if u["kind"] == "claims_unaccounted"][0]
    assert "proof partial" in job.verdict and "no_verification_runner" in job.verdict
    # it rides the job document and the mirror, so a restart still has it
    assert job.to_dict()["proof"]["identity"] == proof["identity"]
    dispatch._jobs.clear()
    assert dispatch.get(job.id).proof["identity"] == proof["identity"]


async def test_the_setting_off_leaves_the_payload_and_the_verdict_exactly_as_they_were(fake_tool, monkeypatch):
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "agent_dispatch_prove" else real(key, default))
    job = await _run_job(fake_tool, monkeypatch)
    result = dispatch.compact(job)["result"]
    assert "proof" not in result and "proof" not in dispatch.compact(job)
    assert job.proof is None and "proof" not in job.to_dict()
    assert job.verdict == ("1/1 workers done · 3 files changed on disk · not verified: verification disabled "
                           "by the request (verify: none)")
    mirror = json.loads((Path(dispatch._data_dir()) / f"{job.id}.json").read_text(encoding="utf-8"))
    assert "proof" not in mirror
