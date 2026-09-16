"""Project-scoped todos + attachment budget (close-the-loop Task 7)."""
from __future__ import annotations

import json

import pytest

import src.agent_tools.coding_tools as ct
from src.agent_harness import attachment_budgeted_text
from src.agent_loop import project_todos_block


@pytest.mark.asyncio
async def test_todowrite_also_writes_project_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path))
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda k, d=None: True if k == "agent_project_todos" else d,
    )
    tool = ct.TodoWriteTool()
    r = await tool.execute(
        json.dumps({
            "todos": [
                {"content": "Inspect code", "status": "pending", "priority": "high"},
            ]
        }),
        {"session_id": "chat1", "project_id": "proj-abc"},
    )
    assert r["exit_code"] == 0
    session = json.loads((tmp_path / "chat1.json").read_text(encoding="utf-8"))
    project = json.loads((tmp_path / "project-proj-abc.json").read_text(encoding="utf-8"))
    assert session["todos"][0]["content"] == "Inspect code"
    assert project["todos"][0]["content"] == "Inspect code"


def test_new_chat_prompt_includes_incomplete_project_todos(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path))
    ct.save_project_todos("p1", [
        {"content": "Diagnose giant layers", "status": "in_progress", "priority": "high"},
        {"content": "Verify all fixes in browser", "status": "pending", "priority": "high"},
        {"content": "Done already", "status": "completed", "priority": "low"},
    ])
    block = project_todos_block(ct.load_project_todos("p1"))
    assert "Incomplete work" in block
    assert "Diagnose giant layers" in block
    assert "Verify all fixes in browser" in block
    assert "Done already" not in block
    assert project_todos_block(ct.load_project_todos("empty")) == ""


def test_attachment_budget_keeps_headings_drops_body():
    blob = "Keep implementing.\n=== File: plan.md ===\n# Task 08\n" + ("x" * 8000) + "\n# Task 09\nmore"
    out = attachment_budgeted_text(blob, 400)
    assert "Task 08" in out and "Task 09" in out
    assert "x" * 1000 not in out
    assert "read the section you need" in out.lower() or "attachment" in out.lower()
    assert "Keep implementing." in out
