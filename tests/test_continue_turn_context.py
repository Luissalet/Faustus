"""Continue-turn injects working state, not the spec (close-the-loop Task 7b)."""
from __future__ import annotations

from src.agent_loop import _looks_like_continue_turn, continue_turn_block
from src.agent_harness import attachment_budgeted_text
from src.agent_tools import coding_tools as ct


def test_keep_implementing_is_a_continue_turn():
    assert _looks_like_continue_turn("Keep implementing the plan")
    assert _looks_like_continue_turn("Continue the implementation")
    assert _looks_like_continue_turn("sigue el plan")
    assert _looks_like_continue_turn("sigue implementando")
    assert _looks_like_continue_turn("Continue")
    assert not _looks_like_continue_turn("what does keep implementing mean in this file?")
    assert not _looks_like_continue_turn("Añade un botón de borrar")


def test_continue_block_lists_incomplete_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path))
    ct.save_project_todos("p1", [
        {"content": "Diagnose giant layers", "status": "in_progress", "priority": "high"},
        {"content": "Verify all fixes in browser", "status": "pending", "priority": "high"},
        {"content": "Done already", "status": "completed", "priority": "low"},
    ])
    ct.save_project_working_set(
        "p1",
        last_files=["static/editor/viewport2d.js"],
        last_tools=[{"tool": "edit_file", "ok": True, "paths": ["static/editor/viewport2d.js"]}],
        last_error="",
    )
    block = continue_turn_block(ct.load_project_todos("p1"), ct.load_project_working_set("p1"))
    assert "Diagnose giant layers" in block
    assert "Verify all fixes in browser" in block
    assert "Done already" not in block
    assert "viewport2d.js" in block


def test_attachment_budget_applied_to_user_message():
    blob = "Keep implementing.\n=== File: plan.md ===\n# Task 08\n" + ("x" * 8000)
    out = attachment_budgeted_text(blob, 400)
    assert "Task 08" in out
    assert "x" * 1000 not in out
    assert "Keep implementing." in out
