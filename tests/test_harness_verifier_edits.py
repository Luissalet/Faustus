"""A turn that edits the very script it uses to check its work is flagged.

Coaching (FAUSTUS §90): asked to make a check pass, the agent rewrote the
check. Run, edit, run again is that shape; writing a test and then running
it is not. The flag is advisory like every claim check (7bfba264).
"""
import json

from src.agent_harness import TurnLedger

OK = {"output": "ok", "exit_code": 0}


def _edit(ledger, path):
    ledger.record("edit_file", json.dumps({"path": path, "old_string": "a", "new_string": "b"}), {"output": "edited"})


def test_check_run_then_edited_then_rerun_is_flagged(tmp_path):
    ledger = TurnLedger(str(tmp_path), "Make the export pass cults3d_check.py.")
    ledger.record("bash", "python tools/cults3d_check.py out/", {"output": "FAIL 3", "exit_code": 1})
    _edit(ledger, "tools/cults3d_check.py")
    ledger.record("bash", "python tools/cults3d_check.py out/", OK)
    assert ledger.edited_verifiers() == ["tools/cults3d_check.py"]
    check = ledger.check_completion("La exportación ya pasa la comprobación.")
    assert "verifier_edited" in check["reasons"] and not check["ok"]
    assert check["verifier_edits"] == ["tools/cults3d_check.py"]
    assert "cults3d_check.py" in ledger.advisory_note(check)
    assert ledger.record_claim_advisory(check, 3)["verifier_edits"] == ["tools/cults3d_check.py"]


def test_test_written_first_and_then_run_is_not_flagged(tmp_path):
    ledger = TurnLedger(str(tmp_path), "Add a test for cart.py and run it.")
    _edit(ledger, "tests/test_cart.py")
    ledger.record("bash", "python -m pytest tests/test_cart.py", OK)
    assert ledger.edited_verifiers() == []


def test_editing_other_code_between_two_check_runs_is_not_flagged(tmp_path):
    ledger = TurnLedger(str(tmp_path), "Fix cart.py so check.py passes.")
    ledger.record("bash", "python check.py", {"output": "FAIL", "exit_code": 1})
    _edit(ledger, "cart.py")
    ledger.record("bash", "python check.py", OK)
    assert ledger.edited_verifiers() == []
    assert "verifier_edited" not in ledger.check_completion("He arreglado cart.py.")["reasons"]
