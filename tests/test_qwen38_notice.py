"""Long-run qwen3.8 notice (close-the-loop Task 8)."""
from src.agent_loop import qwen38_long_run_notice


def test_qwen38_notice_after_forty_rounds():
    msg = qwen38_long_run_notice("qwen3.8:27b", round_num=40, compacted=False, already=False)
    assert msg
    assert "qwen3-coder" in msg.lower()


def test_qwen38_notice_once_per_run():
    assert qwen38_long_run_notice("qwen3.8:27b", 40, False, already=True) is None
    assert qwen38_long_run_notice("llama3.1:8b", 50, True, already=False) is None
    assert qwen38_long_run_notice("qwen3.8:27b", 12, False, already=False) is None
    assert qwen38_long_run_notice("qwen3.8:27b", 12, True, already=False)
