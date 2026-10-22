"""The `criteria_check` tool and the rules that decide a contract criterion's
state (src/agent_tools/contract_tools.py, src/verification.py
verify_contract_link, src/contract_criteria.py CriteriaBook)."""
import asyncio
import json
import os

import pytest

from src import contract_criteria as cc
from src import verification as ver
from src.agent_tools.contract_tools import CriteriaCheckTool

SPEC = """# Archive tool

- `pack`, `unpack` and `scan` stream: their memory use must stay under 64 MB for any input size.
- A damaged archive fails with `corrupt` and exit code 1.
- Every command prints exactly one JSON document on stdout.
- The tool must never delete the source files.
- Every reply must arrive within 5 ms.
- The help command exits with status 0.
- Logs must use the format `YYYY-MM-DD`.
- Temporary files must be removed.
"""


@pytest.fixture
def tracker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.plan_tracker.PLAN_TRACKER_DIR", str(tmp_path))
    return tmp_path


def _book(scope=None, strict=True, session_id=None):
    book = cc.CriteriaBook.from_message(SPEC, min_chars=0, scope=scope, strict=strict, session_id=session_id)
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


def _call(book, cid, command, output, exit_code=0, tool="bash", kind="shell"):
    book.record_call(cid, tool, json.dumps({"command": command}), {"output": output, "exit_code": exit_code},
                     kind=kind, ok=exit_code == 0)


def test_book_extracts_one_criterion_per_streaming_subject():
    book = _book()
    assert {c["subject"] for c in book.criteria if c["subject"]} == {"pack", "unpack", "scan"}
    assert book.counts()["pending"] == len(book.criteria)


def test_unknown_call_keeps_pending():
    book = _book()
    rows = book.apply_links([{"id": _id(book, needle="`corrupt`"), "call_id": "call_9_9"}])
    assert rows[0]["state"] == "pending" and rows[0]["accepted"] is False
    assert "no tool call" in rows[0]["detail"]


def test_call_before_the_last_change_is_stale():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    _call(book, "c1", "python -m arc unpack bad.arc", '{"error": {"code": "corrupt"}}', 1)
    book.record_call("c2", "edit_file", json.dumps({"path": "arc/core.py"}), {"output": "edited"},
                     kind="mutation", ok=True, paths=["arc/core.py"])
    row = book.apply_links([{"id": cid, "call_id": "c1"}])[0]
    assert row["state"] == "pending" and "stale" in row["detail"]


def test_a_documentation_only_write_does_not_make_checks_stale():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    _call(book, "c1", "python -m arc unpack bad.arc", '{"error": {"code": "corrupt"}}', 1)
    book.record_call("c2", "write_file", json.dumps({"path": "README.md"}), {"output": "written"},
                     kind="mutation", ok=True, paths=["README.md"])
    assert book.apply_links([{"id": cid, "call_id": "c1"}])[0]["state"] == "proven"


def test_check_of_one_subject_does_not_prove_another():
    book = _book()
    _call(book, "c1", "python measure.py pack big.bin", "pack: peak 31 MB")
    rows = book.apply_links([{"id": _id(book, subject="pack"), "call_id": "c1"},
                             {"id": _id(book, subject="unpack"), "call_id": "c1"}])
    assert rows[0]["state"] == "proven"
    assert rows[1]["state"] == "pending" and "unpack" in rows[1]["detail"]


def test_the_command_text_is_not_evidence_only_the_output_is():
    book = _book()
    # names the subject and the unit in the command, prints nothing about them
    _call(book, "c1", "python measure.py unpack big.bin --limit 64MB", "ok")
    row = book.apply_links([{"id": _id(book, subject="unpack"), "call_id": "c1"}])[0]
    assert row["state"] == "pending" and "output" in row["detail"]


@pytest.mark.parametrize("command,tool", [
    ('echo "unpack streams under 64 MB: ok"', "bash"),
    ("printf 'unpack peak 12 MB\\n'", "bash"),
    ("Write-Output 'unpack peak 12 MB'", "powershell"),
    ("Write-Host unpack peak 12 MB; echo done", "powershell"),
    ("cat <<EOF\nunpack peak 12 MB\nEOF", "bash"),
    ("print('unpack peak 12 MB')", "python"),
])
def test_a_bare_echo_is_never_evidence(command, tool):
    for strict in (True, False):
        book = _book(strict=strict)
        _call(book, "c1", command, "unpack peak 12 MB", tool=tool)
        row = book.apply_links([{"id": _id(book, subject="unpack"), "call_id": "c1"}])[0]
        assert row["state"] == "pending" and "only prints text" in row["detail"]


def test_an_echo_after_a_real_command_is_fine():
    book = _book()
    _call(book, "c1", "python measure.py unpack big.bin && echo done", "unpack: peak 12 MB\ndone")
    assert book.apply_links([{"id": _id(book, subject="unpack"), "call_id": "c1"}])[0]["state"] == "proven"


def test_numbers_count_only_with_their_unit_or_a_comparison():
    book = _book()
    cid = _id(book, needle="within 5 ms")
    _call(book, "c1", "pytest -q", "5 passed in 0.31s")
    assert book.apply_links([{"id": cid, "call_id": "c1"}])[0]["state"] == "pending"
    _call(book, "c2", "python bench.py", "p99 latency 3.2 ms")
    assert book.apply_links([{"id": cid, "call_id": "c2"}])[0]["state"] == "proven"
    other = _book()
    oid = _id(other, needle="within 5 ms")
    _call(other, "c1", "python bench.py --check", "p99 <= 5 (budget)")
    assert other.apply_links([{"id": oid, "call_id": "c1"}])[0]["state"] == "proven"
    mib = _book()
    _call(mib, "c1", "python measure.py scan big.bin", "scan peak 40 MiB")
    assert mib.apply_links([{"id": _id(mib, subject="scan"), "call_id": "c1"}])[0]["state"] == "proven"


def test_an_exit_status_criterion_is_shown_by_the_exit_code():
    book = _book()
    cid = _id(book, needle="exits with status 0")
    _call(book, "c1", "python -m arc help", "usage: arc ...", 0)
    assert book.apply_links([{"id": cid, "call_id": "c1"}])[0]["state"] == "proven"
    other = _book()
    _call(other, "c1", "python -m arc help", "usage: arc ...", 2)
    assert other.apply_links([{"id": _id(other, needle="exits with status 0"), "call_id": "c1"}])[0]["state"] == "pending"


def test_error_criterion_is_proven_by_a_failing_call_that_shows_the_code():
    book = _book()
    cid = _id(book, needle="`corrupt`")
    _call(book, "call_4_0", "python -m arc unpack damaged.arc", '{"error": {"code": "corrupt", "message": "crc"}}', 1)
    row = book.apply_links([{"id": cid, "command": "unpack damaged"}])[0]
    assert row["state"] == "proven"
    assert book.get(cid)["evidence_refs"] == ["call_4_0"]


def test_a_call_that_shows_none_of_the_terms_is_not_accepted():
    book = _book()
    cid = _id(book, needle="never delete")
    _call(book, "c1", "ls", "a b")
    row = book.apply_links([{"id": cid, "call_id": "c1"}])[0]
    assert row["state"] == "pending" and "none of the criterion's terms" in row["detail"]


def test_not_applicable_needs_a_reason_and_unmet_is_taken_as_said():
    book = _book()
    cid = _id(book, needle="never delete")
    rows = book.apply_links([{"id": cid, "status": "not_applicable"}])
    assert rows[0].get("error") and book.get(cid)["state"] == "pending"
    book.apply_links([{"id": cid, "status": "not_applicable", "reason": "the tool only reads sources"}])
    assert book.get(cid)["state"] == "not_applicable"
    assert cid not in [c["id"] for c in book.open()]
    other = _id(book, needle="exactly one JSON")
    book.apply_links([{"id": other, "status": "unmet", "reason": "scan prints two documents"}])
    assert book.get(other)["state"] == "unmet"


def test_mass_not_applicable_keeps_the_claims_open():
    book = _book()
    ids = [c["id"] for c in book.criteria]
    quarter = int(len(ids) * cc.NA_FRACTION)
    book.apply_links([{"id": i, "status": "not_applicable", "reason": "covered"} for i in ids[:quarter]])
    assert not book.na_mass()
    book.apply_links([{"id": ids[quarter], "status": "not_applicable", "reason": "covered"}])
    assert book.na_mass()
    open_ids = {c["id"] for c in book.open()}
    assert set(ids[:quarter + 1]) <= open_ids
    summary = book.summary()
    assert summary["mass_not_applicable"] and summary["claimed_not_applicable"] == ids[:quarter + 1]
    assert summary["open_total"] == len(ids)
    note = book.audit_note("BASE")
    assert "Claimed not applicable" in note and ids[0] in note


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
    _call(book, "c1", "python measure.py scan big.bin", "scan peak 12 MB")
    book.apply_links([{"id": cid, "call_id": "c1"}])
    assert book.get(cid)["state"] == "proven"
    book.apply_links([{"id": cid, "call_id": "nope"}])
    assert book.get(cid)["state"] == "proven"


def test_lenient_evidence_only_needs_the_call():
    book = _book(strict=False)
    cid = _id(book, subject="unpack")
    _call(book, "c1", "ls", "")
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
    _call(book, "c1", "python -m arc unpack bad.arc", "corrupt", 1)
    out = _run({"links": [{"id": cid, "call_id": "c1"}, {"id": "C99", "call_id": "c1"}]}, book)
    assert out["exit_code"] == 0
    assert out["results"][0]["state"] == "proven" and out["results"][1]["error"] == "no such criterion"
    assert out["criteria"]["proven"] == 1 and cid not in out["open"]
    assert "Recent calls of this turn: c1 bash" in out["output"]
    listing = _run({"list": True}, book)
    assert listing["exit_code"] == 0 and cid not in listing["output"].split("\n\n")[0]
    everything = _run({"list": True, "only_open": False}, book)
    assert cid in everything["output"]
    limits = _run({"list": True, "kind": "limit"}, book)
    assert "within 5 ms" in limits["output"] and "`corrupt`" not in limits["output"].split("\n\n")[0]
    assert _run({}, book)["exit_code"] == 1
    assert asyncio.run(CriteriaCheckTool().execute("{}", {}))["exit_code"] == 1


def test_only_unmet_and_not_applicable_carry_over_to_a_rerun(tracker_dir):
    book = _book(scope="proj-b")
    scan = _id(book, subject="scan")
    na = _id(book, needle="never delete")
    bad = _id(book, needle="exactly one JSON")
    _call(book, "c1", "python measure.py scan big.bin", "scan peak 12 MB")
    _run({"links": [{"id": scan, "call_id": "c1"},
                    {"id": na, "status": "not_applicable", "reason": "read-only tool"},
                    {"id": bad, "status": "unmet", "reason": "two documents"}]}, book)
    assert len(list((tracker_dir / "proj-b" / "criteria").glob("*.json"))) == 1
    again = cc.CriteriaBook.from_message(SPEC, min_chars=0, scope="proj-b")
    assert again.loaded
    assert again.get(scan)["state"] == "pending" and "earlier turn" in again.get(scan)["detail"]
    assert again.get(na)["state"] == "not_applicable" and again.get(bad)["state"] == "unmet"
    resumed = cc.latest("proj-b")
    resumed.start_new_turn()
    assert resumed.get(scan)["state"] == "pending" and resumed.get(na)["state"] == "not_applicable"
    from src import plan_tracker
    assert plan_tracker.active("proj-b") is None


def test_latest_only_resumes_the_same_session_and_prunes_old_books(tracker_dir):
    a = _book(scope="proj-c", session_id="A")
    a.save()
    assert cc.latest("proj-c", session_id="B") is None
    assert cc.latest("proj-c", session_id="A") is not None
    path = cc._book_path("proj-c", a.key)
    old = os.path.getmtime(path) - cc.BOOK_MAX_AGE_S - 10
    os.utime(path, (old, old))
    assert cc.latest("proj-c", session_id="A") is None
    assert not os.path.exists(path)


def test_save_skips_incognito_and_merges_newer_states(tracker_dir):
    secret = _book(scope="proj-d")
    secret.persist = False
    assert secret.save() is False
    assert not (tracker_dir / "proj-d").exists()
    one = _book(scope="proj-e")
    two = _book(scope="proj-e")
    cid = _id(one, needle="never delete")
    one.apply_links([{"id": cid, "status": "not_applicable", "reason": "reads only"}])
    assert one.save()
    assert two.save()                         # older in memory, newer on disk wins for that criterion
    assert cc.load("proj-e", one.key).get(cid)["state"] == "not_applicable"
    leftovers = [n for n in os.listdir(tracker_dir / "proj-e" / "criteria") if n.endswith(".tmp")]
    assert leftovers == []


def test_a_malformed_stored_book_is_coerced():
    bad = cc.CriteriaBook.from_dict({"criteria": [
        {"id": "c01", "quote": 123, "subject": 7, "state": "weird", "line": "x", "keys": "nope"},
        {"id": "C02", "quote": "It must work.", "section": None, "state": "proven"}]})
    assert bad.get("C01")["quote"] == "123" and bad.get("C01")["subject"] == "7"
    assert bad.get("C01")["state"] == "pending" and bad.get("C01")["keys"] == []
    for fn in (bad.summary, bad.start_note, bad.gap_note, lambda: bad.audit_note("BASE"),
               lambda: bad.list_page()):
        fn()


def test_audit_note_keeps_the_generic_text_and_appends_the_open_criteria():
    book = _book()
    _call(book, "call_3_0", "pytest -q", "3 passed")
    note = book.audit_note("GENERIC AUDIT TEXT")
    assert note.startswith("GENERIC AUDIT TEXT\n\n")
    assert "criteria_check" in note and book.shown
    for c in book.criteria:
        assert c["id"] in note
    assert "[unpack]" in note and "one check per subject" in note
    assert "call_3_0 bash: pytest -q" in note
    book.apply_links([{"id": c["id"], "status": "not_applicable", "reason": "r"} for c in book.criteria[:2]])
    assert book.criteria[0]["id"] + " " not in book.gap_note()
    start = book.start_note(max_chars=600)
    assert len(start) <= 600 and "criteria_check" in start


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


def test_completion_gate_reports_open_criteria_the_model_was_shown():
    from src.agent_harness import completion_gate
    base = {"stop_reason": "complete", "tests": None, "ui_smoke": None}
    assert completion_gate(base) is None
    shown = {"shown": True}
    assert completion_gate({**base, "criteria": {**shown, "open_total": 0, "proven": 3}}) is None
    assert completion_gate({**base, "criteria": {**shown, "open_total": 2}}) == "criteria_unverified"
    assert completion_gate({**base, "criteria": {**shown, "pending": 0, "unmet": 1}}) == "criteria_unverified"
    # not shown (attended `always` turn): reported, not gated
    assert completion_gate({**base, "criteria": {"shown": False, "open_total": 4}}) is None
    assert completion_gate({**base, "stop_reason": "budget", "criteria": {**shown, "open_total": 2}}) is None


def test_plan_done_compares_task_files_with_the_turn_mutations_normalised(tracker_dir, monkeypatch):
    from src import plan_tracker as pt
    from src.agent_tools.plan_tools import PlanDoneTool
    body = ("## Task 1: core\nWrite `src/core.py` and `src/util.py`.\n\n## Task 2: docs\nWrite docs.\n\n"
            "## Task 3: tests\nWrite tests.\n")
    tracker = pt.upsert_from_attachment("scope-x", "plan.md", body)
    assert tracker
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: None, raising=False)
    task = tracker["tasks"][0]
    ctx = {"project_id": "scope-x", "ledger_mutations": ["SRC\\core.py"], "turn_id": "turn-2"}
    out = asyncio.run(PlanDoneTool().execute(json.dumps({"id": task["id"], "evidence": "ran pytest -q, 4 passed"}), ctx))
    assert out.get("sealed"), out
    assert task["files"] == ["src/core.py", "src/util.py"]
    assert out.get("unverified_files") == ["src/util.py"]
    assert "not changed in this turn" in out["unverified_files_note"]


def test_plan_done_does_not_flag_files_of_a_task_started_in_an_earlier_turn(tracker_dir, monkeypatch):
    from src import plan_tracker as pt
    from src.agent_tools.plan_tools import PlanDoneTool, PlanNextTool
    body = ("## Task 1: core\nWrite `src/core.py`.\n\n## Task 2: docs\nWrite docs.\n\n"
            "## Task 3: tests\nWrite tests.\n")
    tracker = pt.upsert_from_attachment("scope-y", "plan.md", body)
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: None, raising=False)
    asyncio.run(PlanNextTool().execute("{}", {"project_id": "scope-y", "turn_id": "turn-1"}))
    ctx = {"project_id": "scope-y", "ledger_mutations": [], "turn_id": "turn-2"}
    out = asyncio.run(PlanDoneTool().execute(
        json.dumps({"id": tracker["tasks"][0]["id"], "evidence": "ran pytest -q, 4 passed"}), ctx))
    assert out.get("sealed") and "unverified_files" not in out
