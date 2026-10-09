from types import SimpleNamespace

from src.coordination.continuation import objective


def test_a_steer_during_an_approval_resume_keeps_the_original_objective():
    request = "Fix the code, verify it, checkpoint and submit it for review."
    approval = SimpleNamespace(pending=SimpleNamespace(continuation_query=request))
    assert objective("The compiler is in this other folder.", approval) == request
    second = SimpleNamespace(pending=SimpleNamespace(continuation_query=objective("Do not repeat the heartbeat.", approval)))
    assert objective("Use the new test flag.", second) == request


def test_a_fresh_turn_captures_the_actual_request_not_a_retrieval_summary():
    assert objective("Review the new task and deliver a concrete result.") == "Review the new task and deliver a concrete result."
    assert objective("A replacement task", SimpleNamespace(pending=SimpleNamespace(continuation_query=""))) == "A replacement task"


def test_approval_creation_uses_the_original_request_receipt():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "src/agent_loop.py").read_text(encoding="utf-8")
    assert "continuation_query=_continuation_objective" in source
    assert "continuation_query=_retrieval_query" not in source
