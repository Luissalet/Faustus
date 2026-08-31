"""edit_file: a miss on old_string names the closest real region so a local
model can copy the exact text instead of guessing again."""

import asyncio
import os

from src.agent_tools.filesystem_tools import EditFileTool
from src import tool_execution


def _run(coro):
    return asyncio.run(coro)


class _bound:
    """Resolve model paths under tmp_path. Patches the resolver itself (instead
    of the workspace contextvars) so the test is immune to allowed-roots state
    other tests leave behind in the process."""

    def __init__(self, root):
        self.root = str(root)

    def __enter__(self):
        # Other tests importlib.reload() src.tool_execution: patch the module
        # object the tools import from *now*, not the one bound at collection.
        import importlib
        self._mod = importlib.import_module("src.tool_execution")
        self._orig = self._mod._resolve_tool_path
        root = self.root
        self._mod._resolve_tool_path = lambda raw: os.path.realpath(os.path.join(root, str(raw)))
        self._t1 = self._mod._active_workspace.set(root)

    def __exit__(self, *exc):
        self._mod._active_workspace.reset(self._t1)
        self._mod._resolve_tool_path = self._orig


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
    with _bound(tmp_path):
        res = _run(EditFileTool().execute(
            '{"path": "projects.js", "old_string": "<span class=\\"project-card-name\\">${escapeHtml(project.title)}</span>", "new_string": "x"}',
            {},
        ))
    assert res["exit_code"] == 1
    assert "Closest match" in res["error"] and "line 3" in res["error"]
    assert "project-card-name" in res["error"]


def test_edit_file_not_found_without_similar_text(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    with _bound(tmp_path):
        res = _run(EditFileTool().execute('{"path": "a.py", "old_string": "def totally_unrelated_function():", "new_string": "y"}', {}))
    assert res["exit_code"] == 1 and "Nothing similar found" in res["error"]
