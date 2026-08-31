"""edit_file: a miss on old_string names the closest real region so a local
model can copy the exact text instead of guessing again."""

import asyncio
import os

from src.agent_tools.filesystem_tools import EditFileTool
from src import tool_execution


def _run(coro):
    return asyncio.run(coro)


def test_edit_file_not_found_points_at_closest_lines(tmp_path, monkeypatch):
    target = tmp_path / "projects.js"
    target.write_text(
        "export function cardHtml(project) {\n"
        "  return `<button class=\"project-card\" data-id=\"${project.id}\">\n"
        "    <span class=\"project-card-name\">${escapeHtml(project.name)}</span>\n"
        "  </button>`;\n"
        "}\n",
        encoding="utf-8",
    )
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        res = _run(EditFileTool().execute(
            '{"path": "projects.js", "old_string": "<span class=\\"project-card-name\\">${escapeHtml(project.title)}</span>", "new_string": "x"}',
            {},
        ))
    finally:
        tool_execution._active_workspace.reset(token)
    assert res["exit_code"] == 1
    assert "Closest match" in res["error"] and "line 3" in res["error"]
    assert "project-card-name" in res["error"]


def test_edit_file_not_found_without_similar_text(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        res = _run(EditFileTool().execute('{"path": "a.py", "old_string": "def totally_unrelated_function():", "new_string": "y"}', {}))
    finally:
        tool_execution._active_workspace.reset(token)
    assert res["exit_code"] == 1 and "Nothing similar found" in res["error"]
