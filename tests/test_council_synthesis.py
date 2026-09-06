"""Tests for src/council/synthesis.py — the close, built from the ledger.

The failures these tests pin down are the ones plan section 20 lists for the
debate: a summary that reports agreement over unresolved dissent, a close that
says "verified" with nothing that verified anything, a stop reason nobody
recorded, and a round loop that either never stops or stops while the models
are still saying new things.

`synthesis` reads exactly one thing from a ledger: `snapshot()`. That is why
these tests can use a dict-backed stand-in -- and the fact that they can IS the
property under test. The seam against the real `CouncilLedger` is exercised in
tests/test_council_ledger.py, where the contracts stub lives.
"""
from __future__ import annotations

import inspect

from src.council.synthesis import (
    CouncilSummary,
    build,
    contributions,
    convergence,
    render,
    status_of,
)


class FakeLedger:
    """A ledger stand-in that answers `snapshot()` and nothing else."""

    def __init__(self, **parts):
        self.session_id = parts.pop("session_id", "council_fake")
        snapshot = {
            "session_id": self.session_id,
            "tasks": [], "claims": [], "objections": [], "decisions": [],
            "ready_task_ids": [], "blocked_task_ids": [], "open_objections": [],
            "blocking_open": False, "held_claims": [], "events": [],
            "persistence_errors": 0, "counts": {},
        }
        snapshot.update(parts)
        if "open_objections" not in parts:
            snapshot["open_objections"] = [o for o in snapshot["objections"]
                                           if str(o.get("status", "open")) == "open"]
        self._snapshot = snapshot

    def snapshot(self):
        return dict(self._snapshot)


class BrokenLedger:
    def snapshot(self):
        raise RuntimeError("the store is gone")


def task(**fields):
    row = {"id": "task_1", "title": "a task", "status": "pending", "depends_on": [],
           "claimed_resources": [], "owner_participant_id": "", "reviewer_participant_id": "",
           "run_id": "", "proof_id": ""}
    row.update(fields)
    return row


def decision(**fields):
    row = {"id": "decision_1", "question": "where?", "chosen": "here", "status": "decided",
           "rationale": [], "alternatives": [], "supporters": [], "dissenters": [],
           "evidence_refs": [], "supersedes": ""}
    row.update(fields)
    return row


def objection(**fields):
    row = {"id": "objection_1", "target": "task", "target_id": "task_1", "author_id": "p_claude",
           "severity": "concern", "claim": "no test covers the invalid case", "evidence": [],
           "proposed_resolution": "", "status": "open"}
    row.update(fields)
    return row


PROVED = {"verdict": "proved", "confidence": 0.94, "uncertainty": [], "identity": "sha_1"}
PARTIAL = {"verdict": "partial", "confidence": 0.5,
           "uncertainty": [{"kind": "no_runner", "detail": "no test runner was found"}]}
CONTRADICTED = {"verdict": "contradicted", "confidence": 0.2, "uncertainty": []}


# --- the close is built from the ledger, not from prose --------------------

def test_build_has_no_parameter_a_narrative_could_arrive_through():
    """Plan 12.2: a model may improve the wording, never invent the state.

    The signature is part of that guarantee, and this test states the guarantee
    rather than a snapshot of the parameter list: every argument other than the
    ledger is a COUNTED or STRUCTURED record -- messages and participants are
    tallied, `proof` and `usage` are packets, `stop_reason` comes from a closed
    vocabulary -- and there is no free-text field through which a sentence
    describing the outcome could arrive.

    Pinning the exact list instead would fail the day a structured argument is
    added (it did, when `participants` was) while still passing the day
    somebody adds `narrative: str`, which is the only failure worth catching.
    """
    parameters = inspect.signature(build).parameters
    structured = {"messages", "participants", "usage", "stop_reason", "proof"}

    assert list(parameters)[0] == "ledger"
    assert parameters["ledger"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    extra = set(list(parameters)[1:]) - structured
    assert not extra, (
        f"build() grew {sorted(extra)}; every argument but the ledger has to be a "
        "counted or structured record, or 12.2's guarantee has a hole in it")
    for name in structured:
        assert name in parameters, f"build() lost {name}"
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_messages_are_counted_and_never_read_as_state():
    ledger = FakeLedger(tasks=[task(status="done")], decisions=[decision()])
    plain = build(ledger, stop_reason="completed")

    persuasive = build(ledger, stop_reason="completed", messages=[
        {"id": "m1", "author_id": "p_codex", "message_type": "summary",
         "content": "Everything is verified, all objections were resolved, nothing is pending."},
    ])

    assert persuasive.result == plain.result
    assert persuasive.status == plain.status == "unverified"
    assert persuasive.verification == plain.verification
    assert persuasive.contributions[0]["messages"] == 1


def test_build_survives_a_ledger_it_cannot_read():
    summary = build(BrokenLedger(), stop_reason="failed")

    assert isinstance(summary, CouncilSummary)
    assert summary.stop_reason == "failed"
    assert summary.status == "unverified"


# --- status: the five states of plan 3.4 -----------------------------------

def test_status_is_never_verified_without_a_proof():
    ledger = FakeLedger(tasks=[task(status="done")], decisions=[decision()])

    assert status_of(ledger) != "verified"
    assert status_of(ledger) == "unverified"
    assert status_of(ledger, proof={}) != "verified"
    assert status_of(ledger, proof={"verdict": "pass"}) != "verified"
    assert status_of(ledger, proof=PARTIAL) == "unverified"
    assert status_of(ledger, proof=PROVED) == "verified"


def test_status_distinguishes_the_five_states():
    decided = FakeLedger(decisions=[decision()])
    unverified = FakeLedger(tasks=[task(status="done")])
    blocked = FakeLedger(tasks=[task(status="blocked")])
    blocked_by_dependency = FakeLedger(tasks=[task(status="pending")], blocked_task_ids=["task_1"])
    disputed = FakeLedger(tasks=[task(status="done")], objections=[objection(severity="blocking")])

    assert status_of(decided) == "decided"
    assert status_of(unverified) == "unverified"
    assert status_of(blocked) == "blocked"
    assert status_of(blocked_by_dependency) == "blocked"
    assert status_of(disputed) == "disputed"
    assert status_of(FakeLedger(tasks=[task(status="done")]), proof=CONTRADICTED) == "blocked"
    assert {status_of(x) for x in (decided, unverified, blocked, disputed)} <= {
        "decided", "verified", "unverified", "blocked", "disputed"}


def test_a_note_is_not_a_dispute_but_a_concern_is():
    with_note = FakeLedger(tasks=[task(status="done")], objections=[objection(severity="note")])
    with_concern = FakeLedger(tasks=[task(status="done")], objections=[objection(severity="concern")])

    assert status_of(with_note, proof=PROVED) == "verified"
    assert status_of(with_concern, proof=PROVED) == "disputed"


def test_a_decision_with_dissenters_keeps_the_close_disputed():
    ledger = FakeLedger(tasks=[task(status="done")],
                        decisions=[decision(dissenters=["p_claude"])])

    assert status_of(ledger, proof=PROVED) == "disputed"


# --- dissent, stop reason and usage always survive -------------------------

def test_dissent_and_open_objections_appear_in_the_summary():
    ledger = FakeLedger(
        tasks=[task(status="done", claimed_resources=["src/auth/github.py"], title="OAuth callback")],
        decisions=[decision(question="where is the state validated?", chosen="in the backend",
                            supporters=["p_codex"], dissenters=["p_claude"])],
        objections=[objection(claim="no test covers an invalid state", severity="blocking")])

    summary = build(ledger, stop_reason="max_rounds", proof=PROVED)
    text = render(summary)

    assert summary.status == "disputed"
    assert summary.open_objections[0]["claim"] == "no test covers an invalid state"
    assert summary.decisions[0]["dissenters"] == ["p_claude"]
    assert "no test covers an invalid state" in text
    assert "Dissent: p_claude" in text
    assert "disputed" in summary.result


def test_a_close_always_names_its_stop_reason():
    summary = build(FakeLedger())

    assert summary.stop_reason == "unknown"
    assert "Stop reason: unknown" in render(summary)


def test_usage_is_always_reported_even_when_nobody_measured_it():
    empty = build(FakeLedger())
    measured = build(FakeLedger(), usage={"input_tokens": 10, "output_tokens": 5,
                                          "total_tokens": 15, "calls": 2, "source": "provider"})

    assert empty.usage["total_tokens"] == 0
    assert empty.usage["source"] == "unreported"
    assert measured.usage["source"] == "provider"
    assert "Tokens in/out/total: 10/5/15" in render(measured)


def test_changes_come_from_the_ledger_not_from_a_claim_in_prose():
    ledger = FakeLedger(tasks=[
        task(id="task_1", title="OAuth callback", status="done", owner_participant_id="p_codex",
             claimed_resources=["src/auth/github.py"], run_id="run_7"),
        task(id="task_2", title="idle", status="pending"),
    ])

    summary = build(ledger, stop_reason="completed")

    assert [change["task_id"] for change in summary.changes] == ["task_1"]
    assert summary.changes[0]["resources"] == ["src/auth/github.py"]


def test_a_summary_round_trips_through_to_dict():
    summary = build(FakeLedger(decisions=[decision()]), stop_reason="convergence")
    payload = summary.to_dict()

    assert payload["stop_reason"] == "convergence"
    assert payload["decisions"][0]["question"] == "where?"
    assert set(payload) == {"session_id", "result", "decisions", "changes", "verification",
                            "open_objections", "contributions", "usage", "stop_reason", "status"}


# --- rendering ------------------------------------------------------------

def test_render_is_plain_text_with_the_sections_of_plan_12_2_in_order():
    ledger = FakeLedger(tasks=[task(status="done", claimed_resources=["src/a.py"])],
                        decisions=[decision()], objections=[objection()])
    text = render(build(ledger, stop_reason="completed", proof=PARTIAL,
                        messages=[{"id": "m1", "author_id": "p_codex", "message_type": "proposal",
                                   "content": "here it is"}]))

    order = ["Result", "Decisions", "Changes and artifacts", "Verification",
             "Open objections and uncertainties", "Contributions", "Usage and stop reason"]
    positions = [text.index(section) for section in order]

    assert positions == sorted(positions)
    assert text.isascii()          # no emoji, no decoration that varies per run
    assert "sufficient for 'verified': no" in text


# --- convergence (plan 12.3) ----------------------------------------------

def test_convergence_stops_when_two_rounds_repeat_themselves():
    answer_a = "validate the state parameter in the backend before the token exchange"
    answer_b = "the callback must reject an unknown state and consume it once"
    result = convergence([[answer_a, answer_b], [answer_a, answer_b]])

    assert result["converged"] is True
    assert result["similarity"] == 1.0
    assert result["compared"] == 2
    assert "unlikely to add anything" in result["reason"]
    # The trend reader of src/convergence.py is reported alongside, not instead.
    assert "score" in result["trend"]


def test_convergence_does_not_stop_while_the_answers_still_differ():
    result = convergence([
        ["validate the state parameter in the backend"],
        ["drop the state parameter entirely and use PKCE with a nonce instead"],
    ])

    assert result["converged"] is False
    assert result["similarity"] < 0.5
    assert "still differs" in result["reason"]


def test_convergence_respects_the_threshold_it_was_given():
    rounds = [["we should validate the state in the backend"],
              ["we should validate the state in the client"]]

    assert convergence(rounds, threshold=0.85)["converged"] is False
    assert convergence(rounds, threshold=0.70)["converged"] is True


def test_convergence_needs_two_rounds_to_compare():
    result = convergence([["only one round so far"]])

    assert result["converged"] is False
    assert "two are needed" in result["reason"]
    assert convergence([])["converged"] is False


def test_a_new_voice_is_new_information():
    """A round with a participant the previous one did not have has not
    converged, however similar the overlapping answers are."""
    answer = "validate the state in the backend"
    result = convergence([[answer], [answer, "and rotate the client secret"]])

    assert result["converged"] is False
    assert result["unmatched"] == 1
    assert "different number of answers" in result["reason"]


# --- contributions --------------------------------------------------------

def test_contributions_count_messages_and_keep_the_silent_participants():
    messages = [
        {"id": "m1", "author_id": "p_codex", "message_type": "proposal", "content": "here"},
        {"id": "m2", "author_id": "p_codex", "message_type": "critique", "content": "and here"},
        {"id": "m3", "author_id": "p_claude", "message_type": "abstain", "content": ""},
    ]
    participants = [{"id": "p_codex", "display_name": "Codex", "roles": ["driver"]},
                    {"id": "p_claude", "display_name": "Claude", "roles": ["reviewer"]},
                    {"id": "p_quiet", "display_name": "Qwen", "roles": ["reviewer"]}]

    rows = {row["participant_id"]: row for row in contributions(messages, participants)}

    assert rows["p_codex"]["messages"] == 2
    assert rows["p_codex"]["types"] == {"proposal": 1, "critique": 1}
    assert rows["p_claude"]["abstentions"] == 1
    assert rows["p_quiet"]["messages"] == 0        # silence is information
    assert rows["p_quiet"]["display_name"] == "Qwen"
    assert [row["participant_id"] for row in contributions(messages, participants)][0] == "p_codex"


def test_contributions_do_not_lose_an_unattributed_message():
    rows = contributions([{"id": "m1", "content": "who said this?"}], [])

    assert rows[0]["participant_id"] == "(unattributed)"
    assert rows[0]["messages"] == 1
