"""turn_review: a chat's recent turns, read back from their saved metrics."""
import asyncio
from types import SimpleNamespace

from src import turn_review
from src.agent_tools.turn_review_tool import TurnReviewTool


def _history():
    fail = {"tool": "bash", "round": 2, "exit_code": 1, "output": "ModuleNotFoundError: No module named 'x'"}
    return [
        {"role": "user", "content": "run the tests"},
        {"role": "assistant", "content": "Could not run them.", "metadata": {
            "model": "qwen", "agent_rounds": 14, "total_time": 300.0,
            "tool_events": [dict(fail), dict(fail, round=3), {"tool": "read_file", "round": 1, "exit_code": 0,
                                                              "duration_ms": 1200}],
            "prompt_cache": {"rounds": 14, "processed": 9000, "cached": 1000, "lost_rounds": 5},
        }},
    ]


def test_review_finds_the_repeated_failure_the_lost_cache_and_no_writes():
    r = turn_review.review(_history(), 1)[0]
    assert r["question"] == "run the tests" and r["tool_calls"] == 3 and r["writes"] == 0
    text = " ".join(r["findings"])
    assert "failed 2 times the same way" in text
    assert "nothing written" in text
    assert "lost in 5 of 14" in text
    md = turn_review.render([r])
    assert "**finding**" in md and "bash" in md


def test_the_tool_reads_this_chat_and_refuses_someone_elses(monkeypatch):
    sess = SimpleNamespace(owner="alice", history=_history())
    sm = SimpleNamespace(get_session=lambda sid: sess if sid == "s1" else None)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: sm)
    ok = asyncio.run(TurnReviewTool().execute("", {"session_id": "s1", "owner": "alice"}))
    assert ok["exit_code"] == 0 and "run the tests" in ok["output"]
    other = asyncio.run(TurnReviewTool().execute('{"session_id": "s1"}', {"session_id": "s2", "owner": "bob"}))
    assert other["exit_code"] == 1


def test_empty_chat():
    assert "No agent turn" in turn_review.render(turn_review.review([], 3))
