"""FAUSTUS §115 item 4 — the permanent "Tareas grandes" agent-mode strategy
block (src/agent_loop.py:_big_task_strategy_block) must reach the real
system prompt exactly when a workspace is active, and never for a plain
chat turn with no workspace."""

from src import project_instructions as pi
from src.agent_loop import _build_system_prompt, _big_task_strategy_block


def _system_text(workspace):
    pi.invalidate()
    built = _build_system_prompt(
        [{"role": "user", "content": "hola"}],
        "qwen3:8b", None, None,
        workspace=str(workspace) if workspace else None,
        suppress_skills=True,
    )
    messages = built[0] if isinstance(built, tuple) else built
    return "".join(
        m.get("content", "") for m in messages
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )


def test_block_under_token_budget():
    # ~120 tokens is roughly 480-600 chars for this kind of prose; keep a
    # generous but real ceiling so the block can never grow unnoticed.
    assert len(_big_task_strategy_block()) < 900


def test_block_present_in_agent_workspace_mode(tmp_path):
    pi.invalidate()
    text = _system_text(tmp_path)
    assert "Tareas grandes" in text
    pi.invalidate()


def test_block_absent_without_a_workspace():
    pi.invalidate()
    text = _system_text(None)
    assert "Tareas grandes" not in text
    pi.invalidate()
