"""edit_file: a miss on old_string names the closest real region so a local
model can copy the exact text instead of guessing again."""

import asyncio
import json
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


def test_edit_file_matches_lf_quote_in_crlf_file_and_keeps_crlf(tmp_path):
    """Windows files: the model quotes text with \\n; the file has \\r\\n. The
    edit must apply and the file must keep CRLF everywhere (no whole-file
    line-ending rewrite)."""
    target = tmp_path / "server.py"
    target.write_bytes(b"import os\r\n\r\ndef main():\r\n    return 1\r\n")
    with _bound(tmp_path):
        res = _run(EditFileTool().execute(json.dumps({
            "path": "server.py",
            "old_string": "def main():\n    return 1\n",
            "new_string": "def main():\n    return 2\n",
        }), {}))
    assert res["exit_code"] == 0, res
    data = target.read_bytes()
    assert data == b"import os\r\n\r\ndef main():\r\n    return 2\r\n"
    assert b"\n" not in data.replace(b"\r\n", b"")  # no stray LF-only lines
    assert "+    return 2" in res["diff"]["text"]


def test_edit_file_keeps_lf_files_lf(tmp_path):
    target = tmp_path / "a.js"
    target.write_bytes(b"const a = 1;\nconst b = 2;\n")
    with _bound(tmp_path):
        res = _run(EditFileTool().execute(json.dumps({
            "path": "a.js", "old_string": "const b = 2;", "new_string": "const b = 3;",
        }), {}))
    assert res["exit_code"] == 0, res
    assert target.read_bytes() == b"const a = 1;\nconst b = 3;\n"


def test_apply_patch_keeps_crlf(tmp_path):
    from src.agent_tools.filesystem_tools import ApplyPatchTool
    target = tmp_path / "server.py"
    target.write_bytes(b"import os\r\n\r\ndef main():\r\n    return 1\r\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: server.py\n"
        "@@\n"
        " def main():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch\n"
    )
    with _bound(tmp_path):
        res = _run(ApplyPatchTool().execute(json.dumps({"patch_text": patch}), {}))
    assert res.get("exit_code") == 0, res
    assert target.read_bytes() == b"import os\r\n\r\ndef main():\r\n    return 2\r\n"


def test_write_file_overwrite_keeps_crlf_and_new_files_are_verbatim(tmp_path):
    from src.agent_tools.filesystem_tools import WriteFileTool
    target = tmp_path / "notes.md"
    target.write_bytes(b"# a\r\nb\r\n")
    with _bound(tmp_path):
        res = _run(WriteFileTool().execute(json.dumps({"path": "notes.md", "content": "# a\nc\n"}), {}))
        assert res["exit_code"] == 0, res
        assert target.read_bytes() == b"# a\r\nc\r\n"
        res2 = _run(WriteFileTool().execute(json.dumps({"path": "new/x.py", "content": "x = 1\ny = 2\n"}), {}))
        assert res2["exit_code"] == 0, res2
        assert (tmp_path / "new" / "x.py").read_bytes() == b"x = 1\ny = 2\n"
