import json
import pytest

from src.agent_harness import TurnLedger
from src.agent_loop import _end_turn_with_advisory
from src import question_store


@pytest.mark.parametrize("reason", [
    "plan_no_progress", "plan_continue_cap", "plan_loop_recovery",
    "empty_rounds_exhausted", "no_progress_streak", "turn_wall_clock_ceiling",
])
def test_runtime_stop_preserves_a_real_question_and_reports_unverified_work(tmp_path, reason):
    store = question_store.Store(tmp_path / "questions.sqlite3")
    genuine = store.open("Which source should be used?", session_id="other", owner="alice")
    ledger = TurnLedger()
    ledger.language = "es"
    ledger.progress = [{"content": "leer el archivo elegido", "status": "pending"}]
    events = [json.loads(chunk[6:]) for _, chunk in _end_turn_with_advisory(
        reason=reason, round_num=5, session_id="task", owner="alice", ledger=ledger,
        plan={"done": 0, "total": 3, "key": "t01", "task_title": "leer el archivo elegido", "cap": 40},
    )]
    assert len(events) == 1 and events[0]["type"] == "harness_check"
    assert events[0]["requires_user_input"] is False
    assert store.list_open(owner="alice")[0]["question_id"] == genuine["question_id"]
    assert len(store.list_open(owner="alice")) == 1
    assert ledger.answer_diagnostics[-1]["reason"] == reason
    assert ledger.answer_diagnostics[-1]["advisory"] is True
    assert ledger.verify_outcome()["outcome"] != "verified"
