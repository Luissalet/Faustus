"""The `criteria_check` tool and the rules that decide a contract criterion's
state (src/agent_tools/contract_tools.py, src/verification.py
verify_contract_link, src/contract_criteria.py CriteriaBook)."""
import asyncio
import json

import pytest

from src import contract_criteria as cc
from src import verification as ver
from src.agent_tools.contract_tools import CriteriaCheckTool

SPEC = """# Archive tool

- `pack`, `unpack` and `scan` stream: their memory use must stay under 64 MB for any input size.
- A damaged archive fails with `corrupt` and exit code 1.
- Every command prints exactly one JSON document on stdout.
- The tool must never delete the source files.
"""


@pytest.fixture
def tracker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.plan_tracker.PLAN_TRACKER_DIR", str(tmp_path))
    return tmp_path


def _book(scope=None, strict=True):
    book = cc.CriteriaBook.from_message(SPEC, min_chars=0, scope=scope, strict=strict)
    assert book is not None
    return book


def _id(book, subject=None, needle=None):
    for c in book.criteria:
        if subject is not None and c["subject"] == subject:
            return c["id"]
        if needle is not None and needle in c["quote"] and c["subject"] is None:
            return c["id"]
    raise AssertionError((subject, needle, [x["quote"] for x in book.criteria]))


def _run(tool_args, book):
    return asyncio.run(CriteriaCheckTool().execute(json.dumps(tool_args), {"contract_book": book}))


def test_book_extracts_one_criterion_per_streaming_subject():
    book = _book()
    assert {c["subject"] for c in book.criteria if c["subject"]} == {"pack", "unpack", "scan"}
    assert book.counts()["pending"] == len(book.criteria)


def test_unknown_call_keeps_pending():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    rows = book.apply_links([{"id": cid, "call_id": "call_9_9"}])
    assert rows[0]["state"] == "pending" and rows[0]["accepted"] is False
    assert "no tool call" in rows[0]["detail"]


def test_call_before_the_last_change_is_stale():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    book.record_call("c1", "bash", json.dumps({"command": "python -m arc unpack bad.arc"}),
                     {"output": '{"error": {"code": "corrupt"}}', "exit_code": 1}, kind="shell", ok=False)
    book.record_call("c2", "edit_file", json.dumps({"path": "arc/core.py"}), {"output": "edited"},
                     kind="mutation", ok=True, paths=["arc/core.py"])
    row = book.apply_links([{"id": cid, "call_id": "c1"}])[0]
    assert row["state"] == "pending" and "stale" in row["detail"]


def test_a_documentation_only_write_does_not_make_checks_stale():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    book.record_call("c1", "bash", json.dumps({"command": "python -m arc unpack bad.arc"}),
                     {"output": '{"error": {"code": "corrupt"}}', "exit_code": 1}, kind="shell", ok=False)
    book.record_call("c2", "write_file", json.dumps({"path": "README.md"}), {"output": "written"},
                     kind="mutation", ok=True, paths=["README.md"])
    assert book.apply_links([{"id": cid, "call_id": "c1"}])[0]["state"] == "proven"


def test_check_of_one_subject_does_not_prove_another():
    book = _book()
    book.record_call("c1", "bash", json.dumps({"command": "python measure.py pack big.bin"}),
                     {"output": "peak 31 MB", "exit_code": 0}, kind="shell", ok=True)
    rows = book.apply_links([{"id": _id(book, subject="pack"), "call_id": "c1"},
                             {"id": _id(book, subject="unpack"), "call_id": "c1"}])
    assert rows[0]["state"] == "proven"
    assert rows[1]["state"] == "pending" and "unpack" in rows[1]["detail"]


def test_error_criterion_is_proven_by_a_failing_call_that_shows_the_code():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    book.record_call("call_4_0", "bash", json.dumps({"command": "python -m arc unpack damaged.arc"}),
                     {"output": '{"error": {"code": "corrupt", "message": "crc"}}', "exit_code": 1},
                     kind="shell", ok=False)
    row = book.apply_links([{"id": cid, "command": "unpack damaged"}])[0]
    assert row["state"] == "proven"
    assert book.get(cid)["evidence_refs"] == ["call_4_0"]


def test_a_call_that_mentions_none_of_the_terms_is_not_accepted():
    book = _book()
    cid = _id(book, needle="never delete")
    book.record_call("c1", "bash", json.dumps({"command": "ls"}), {"output": "a b"}, kind="shell", ok=True)
    row = book.apply_links([{"id": cid, "call_id": "c1"}])[0]
    assert row["state"] == "pending" and "none of the criterion's terms" in row["detail"]


def test_not_applicable_needs_a_reason_and_unmet_is_taken_as_said():
    book = _book()
    cid = _id(book, needle="never delete")
    rows = book.apply_links([{"id": cid, "status": "not_applicable"}])
    assert rows[0].get("error") and book.get(cid)["state"] == "pending"
    rows = book.apply_links([{"id": cid, "status": "not_applicable", "reason": "the tool only reads sources"}])
    assert book.get(cid)["state"] == "not_applicable"
    other = _id(book, needle="exactly one JSON")
    book.apply_links([{"id": other, "status": "unmet", "reason": "scan prints two documents"}])
    assert book.get(other)["state"] == "unmet"


def test_the_model_cannot_force_proven_through_any_field():
    book = _book()
    cid = _id(book, needle="exactly one JSON")
    rows = book.apply_links([{"id": cid, "status": "verified"},
                             {"id": cid, "status": "proven", "state": "proven", "evidence_refs": ["x"],
                              "observed": "it prints one JSON document"}])
    assert rows[0].get("error")
    assert book.get(cid)["state"] == "pending"


def test_a_weak_link_never_undoes_a_proven_criterion():
    book = _book()
    cid = _id(book, subject="scan")
    book.record_call("c1", "bash", json.dumps({"command": "python measure.py scan big.bin"}),
                     {"output": "peak 12 MB"}, kind="shell", ok=True)
    book.apply_links([{"id": cid, "call_id": "c1"}])
    assert book.get(cid)["state"] == "proven"
    book.apply_links([{"id": cid, "call_id": "nope"}])
    assert book.get(cid)["state"] == "proven"


def test_lenient_evidence_only_needs_the_call():
    book = _book(strict=False)
    cid = _id(book, subject="unpack")
    book.record_call("c1", "bash", json.dumps({"command": "ls"}), {"output": ""}, kind="shell", ok=True)
    book.record_call("c2", "edit_file", "{}", {"output": "ok"}, kind="mutation", ok=True, paths=["x.py"])
    assert book.apply_links([{"id": cid, "call_id": "c1"}])[0]["state"] == "proven"


def test_verify_contract_link_maps_onto_the_contract_state_names():
    from src.contracts.task import ACCEPTANCE_STATES
    for state in ver.CRITERION_STATES:
        assert ver.contract_state(state) in ACCEPTANCE_STATES
    assert set(cc.STATES) <= set(ACCEPTANCE_STATES)
    crit = {"id": "C01", "subject": None, "keys": ["`x`"], "words": []}
    assert ver.verify_contract_link(crit, None).state == "pending"


def test_tool_links_lists_and_reports_counts(tracker_dir):
    book = _book(scope="proj-a")
    cid = _id(book, needle="`corrupt`")
    book.record_call("c1", "bash", json.dumps({"command": "python -m arc unpack bad.arc"}),
                     {"output": "corrupt", "exit_code": 1}, kind="shell", ok=False)
    out = _run({"links": [{"id": cid, "call_id": "c1"}, {"id": "C99", "call_id": "c1"}]}, book)
    assert out["exit_code"] == 0
    assert out["results"][0]["state"] == "proven" and out["results"][1]["error"] == "no such criterion"
    assert out["criteria"]["proven"] == 1 and cid not in out["open"]
    assert "Recent calls of this turn: c1 bash" in out["output"]
    listing = _run({"list": True}, book)
    assert listing["exit_code"] == 0 and cid in listing["output"]
    assert _run({}, book)["exit_code"] == 1
    assert asyncio.run(CriteriaCheckTool().execute("{}", {}))["exit_code"] == 1


def test_states_persist_beside_the_plan_tracker_and_survive_a_rerun(tracker_dir):
    book = _book(scope="proj-b")
    cid = _id(book, subject="scan")
    book.record_call("c1", "bash", json.dumps({"command": "python measure.py scan big.bin"}),
                     {"output": "peak 12 MB"}, kind="shell", ok=True)
    _run({"links": [{"id": cid, "call_id": "c1"}]}, book)
    files = list((tracker_dir / "proj-b" / "criteria").glob("*.json"))
    assert len(files) == 1
    again = cc.CriteriaBook.from_message(SPEC, min_chars=0, scope="proj-b")
    assert again.loaded and again.get(cid)["state"] == "proven"
    assert cc.latest("proj-b").get(cid)["state"] == "proven"
    # The plan tracker never mistakes the book for a plan.
    from src import plan_tracker
    assert plan_tracker.active("proj-b") is None


def test_audit_note_lists_open_criteria_with_quotes_and_recent_calls():
    book = _book()
    book.record_call("call_3_0", "bash", json.dumps({"command": "pytest -q"}), {"output": "3 passed"},
                     kind="shell", ok=True)
    note = book.audit_note()
    assert "[Harness check" in note and "criteria_check" in note
    for c in book.criteria:
        assert c["id"] in note
    assert "[unpack]" in note and "one check per subject" in note
    assert "call_3_0 bash: pytest -q" in note
    book.apply_links([{"id": c["id"], "status": "not_applicable", "reason": "r"} for c in book.criteria[:2]])
    assert book.criteria[0]["id"] + " " not in book.gap_note()
    start = book.start_note(max_chars=600)
    assert len(start) < 900 and "criteria_check" in start


def test_compact_list_is_bounded():
    text = "\n\n".join(f"Rule {i}: the output must contain the marker `m{i}` exactly once." for i in range(60))
    book = cc.CriteriaBook.from_message(text, min_chars=0)
    note = book.compact_list(max_chars=1000)
    assert len(note) < 1200 and "more (criteria_check" in note


def test_contract_gap_is_a_known_completion_cause_with_a_single_round():
    from src.extra_round_policy import CAUSES, ExtraRounds, decide_extra_round
    assert CAUSES["contract_gap"].family == "completion"
    assert not CAUSES["contract_gap"].outside_budget
    assert decide_extra_round("contract_gap", used=0, limit=1).granted
    assert not decide_extra_round("contract_gap", used=1, limit=1).granted
    xr = ExtraRounds("enforce")
    assert xr.permits("contract_gap", 0, 1) and not xr.permits("contract_gap", 1, 1)
    assert xr.event("contract_gap", used=1, limit=1)["cause"] == "contract_gap"


def test_completion_gate_reports_open_criteria_last():
    from src.agent_harness import completion_gate
    base = {"stop_reason": "complete", "tests": None, "ui_smoke": None}
    assert completion_gate(base) is None
    assert completion_gate({**base, "criteria": {"pending": 0, "unmet": 0, "proven": 3}}) is None
    assert completion_gate({**base, "criteria": {"pending": 2, "unmet": 0}}) == "criteria_unverified"
    assert completion_gate({**base, "criteria": {"pending": 0, "unmet": 1}}) == "criteria_unverified"
    assert completion_gate({**base, "stop_reason": "budget", "criteria": {"pending": 2}}) is None


def test_plan_done_compares_task_files_with_the_turn_mutations_normalised(tracker_dir, monkeypatch):
    from src import plan_tracker as pt
    from src.agent_tools.plan_tools import PlanDoneTool
    body = ("## Task 1: core\nWrite `src/core.py` and `src/util.py`.\n\n## Task 2: docs\nWrite docs.\n\n"
            "## Task 3: tests\nWrite tests.\n")
    tracker = pt.upsert_from_attachment("scope-x", "plan.md", body)
    assert tracker
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: None, raising=False)
    task = tracker["tasks"][0]
    ctx = {"project_id": "scope-x", "ledger_mutations": ["SRC\\core.py"]}
    out = asyncio.run(PlanDoneTool().execute(json.dumps({"id": task["id"], "evidence": "ran pytest -q, 4 passed"}), ctx))
    assert out.get("sealed"), out
    assert task["files"] == ["src/core.py", "src/util.py"]
    assert out.get("unverified_files") == ["src/util.py"]
