"""Tests for `src/structural_search.py` (ast-grep-backed structural search/
rewrite) and the structural_search / structural_rewrite tool wiring.

Skips cleanly (module-level `pytest.importorskip`-style guard) when the
`ast-grep` binary is not installed, since it is an optional dependency
(requirements-optional.txt).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

from src import structural_search as ss
from src import tool_execution as te

_AVAILABLE = ss.available()["available"]
pytestmark = pytest.mark.skipif(
    not _AVAILABLE, reason="ast-grep binary not installed (pip install ast-grep-cli)"
)


PY_SRC = '''def with_log():
    try:
        risky()
    except Exception:
        logging.warning("failed")


def without_log():
    try:
        risky()
    except Exception:
        pass


def call_site():
    foo(1, None)
    foo(2, "keep")
'''

JS_SRC = '''function callSite() {
    foo(1, null);
    foo(2, "keep");
}
'''


def _write(root, rel, content):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", PY_SRC)
    _write(root, "app.js", JS_SRC)
    return str(root)


@pytest.fixture()
def ws(repo):
    """Bind `repo` as the active turn workspace, the same guard
    read_file/grep/code_graph_* are confined by (mirrors tests/test_code_graph.py)."""
    ws_token = te._active_workspace.set(repo)
    roots_token = te._active_workspace_roots.set((repo,))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


# ── available() ─────────────────────────────────────────────────────────

def test_available_reports_binary_and_version():
    info = ss.available()
    assert info["available"] is True
    assert info["path"]
    assert info["version"]


# ── search() ────────────────────────────────────────────────────────────

def test_search_except_without_log_pattern(ws):
    result = ss.search(
        "try:\n    $$$TRY\nexcept Exception:\n    $$$BODY", "python", ws,
    )
    assert result["exit_code"] == 0
    assert result["count"] == 2
    bodies = []
    for h in result["hits"]:
        multi = h["metaVariables"].get("multi", {})
        text = " ".join(b.get("text", "") for b in multi.get("BODY", []))
        bodies.append(text)
    assert any("logging" in b for b in bodies)
    assert any(b.strip() == "pass" for b in bodies)


def test_search_call_pattern_with_metavariables(ws):
    result = ss.search("foo($A, None)", "python", ws)
    assert result["exit_code"] == 0
    assert result["count"] == 1
    hit = result["hits"][0]
    assert hit["metaVariables"]["single"]["A"]["text"] == "1"
    assert "mod.py" in hit["file"]


def test_search_js_pattern(ws):
    result = ss.search("foo($A, null)", "javascript", ws)
    assert result["exit_code"] == 0
    assert result["count"] == 1
    assert "app.js" in result["hits"][0]["file"]


def test_search_ts_lang_accepted(ws):
    ts_path = _write(ws, "app.ts", "function callSite() {\n    foo(1, null);\n}\n")
    result = ss.search("foo($A, null)", "typescript", ts_path)
    # The lang name itself must be accepted (not rejected by _check_lang)
    # and the search must actually run (a single-file path is a valid cwd).
    assert result["exit_code"] == 0
    assert result["count"] == 1


def test_search_no_matches(ws):
    result = ss.search("totally_absent_call($A)", "python", ws)
    assert result["exit_code"] == 0
    assert result["count"] == 0
    assert result["output"] == "no matches"


def test_search_max_results_caps_and_flags_truncation(ws):
    result = ss.search("foo($A, $B)", "python", ws, max_results=1)
    # foo(1, None) and foo(2, "keep") both match foo($A, $B) -> 2 raw hits, capped to 1.
    assert result["count"] == 1
    assert result["truncated"] is True


def test_search_unsupported_lang_is_a_clean_error(ws):
    result = ss.search("foo($A)", "cobol", ws)
    assert result["exit_code"] == 1
    assert "unsupported lang" in result["error"]


def test_search_missing_pattern_is_a_clean_error(ws):
    result = ss.search("", "python", ws)
    assert result["exit_code"] == 1
    assert "pattern is required" in result["error"]


def test_search_path_confinement_rejects_outside_workspace(ws):
    result = ss.search("foo($A)", "python", "/etc")
    assert result["exit_code"] == 1
    assert "outside" in result["error"]


# ── rewrite_preview() / rewrite_apply() ────────────────────────────────

def test_rewrite_preview_does_not_modify_files(ws):
    mod_path = os.path.join(ws, "mod.py")
    before = open(mod_path, encoding="utf-8").read()
    result = ss.rewrite_preview("foo($A, None)", "foo($A)", "python", ws)
    assert result["exit_code"] == 0
    assert result["files_changed"] == [os.path.relpath(mod_path, ws)] or \
        any(f.endswith("mod.py") for f in result["files_changed"])
    assert "foo(1)" in result["output"]
    after = open(mod_path, encoding="utf-8").read()
    assert before == after, "preview must never write to disk"


def test_rewrite_apply_modifies_exactly_the_expected_files(ws):
    mod_path = os.path.join(ws, "mod.py")
    js_path = os.path.join(ws, "app.js")
    before_js = open(js_path, encoding="utf-8").read()

    result = ss.rewrite_apply("foo($A, None)", "foo($A)", "python", mod_path)
    assert result["exit_code"] == 0
    assert result["files_written"] == [os.path.realpath(mod_path)]

    after_py = open(mod_path, encoding="utf-8").read()
    assert "foo(1, None)" not in after_py
    assert "foo(1)" in after_py
    assert 'foo(2, "keep")' in after_py, "non-matching call must be left alone"

    after_js = open(js_path, encoding="utf-8").read()
    assert after_js == before_js, "rewrite scoped to mod.py must not touch app.js"


def test_rewrite_apply_requires_explicit_path(ws):
    result = ss.rewrite_apply("foo($A, None)", "foo($A)", "python", "")
    assert result["exit_code"] == 1
    assert "path is required" in result["error"]


def test_rewrite_preview_no_matches(ws):
    result = ss.rewrite_preview("absent_call($A)", "other($A)", "python", ws)
    assert result["exit_code"] == 0
    assert result["files_changed"] == []
    assert "no matches" in result["output"]


def test_rewrite_missing_rewrite_arg_is_a_clean_error(ws):
    result = ss.rewrite_preview("foo($A, None)", None, "python", ws)
    assert result["exit_code"] == 1
    assert "rewrite is required" in result["error"]


# ── missing binary ──────────────────────────────────────────────────────

def test_missing_binary_error_message(monkeypatch):
    monkeypatch.setattr(ss, "_lookup_done", True)
    monkeypatch.setattr(ss, "_cached_binary", None)
    monkeypatch.setattr(ss, "_cached_version", None)
    info = ss.available()
    assert info["available"] is False
    assert "pip install ast-grep-cli" in info["error"]
    result = ss.search("foo($A)", "python", "")
    assert result["exit_code"] == 1
    assert "pip install ast-grep-cli" in result["error"]


# ── tool wiring ──────────────────────────────────────────────────────────

_NAMES = ("structural_search", "structural_rewrite")


def test_tools_are_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    for name in _NAMES:
        assert name in TOOL_HANDLERS, name
        assert name in TOOL_TAGS, name
        assert name in schema_names, name
        assert name in TOOL_CAPABILITIES, name
        assert name in BUILTIN_TOOL_DESCRIPTIONS, name
        assert len(EXAMPLES.get(name, [])) >= 2, name


def test_structural_search_tool_executor(ws):
    from src.agent_tools.structural_search_tools import StructuralSearchTool

    result = asyncio.run(StructuralSearchTool().execute(
        json.dumps({"pattern": "foo($A, None)", "lang": "python", "path": ws}), {}))
    assert result["exit_code"] == 0
    assert result["count"] == 1


def test_structural_rewrite_tool_executor_preview_then_apply(ws):
    from src.agent_tools.structural_search_tools import StructuralRewriteTool

    mod_path = os.path.join(ws, "mod.py")
    preview = asyncio.run(StructuralRewriteTool().execute(json.dumps({
        "pattern": "foo($A, None)", "rewrite": "foo($A)", "lang": "python", "path": mod_path,
    }), {}))
    assert preview["exit_code"] == 0
    assert "foo(1)" in preview["output"]
    assert open(mod_path, encoding="utf-8").read().count("foo(1, None)") == 1, \
        "apply=false default must not write"

    applied = asyncio.run(StructuralRewriteTool().execute(json.dumps({
        "pattern": "foo($A, None)", "rewrite": "foo($A)", "lang": "python", "path": mod_path,
        "apply": True,
    }), {}))
    assert applied["exit_code"] == 0
    assert "foo(1, None)" not in open(mod_path, encoding="utf-8").read()


def test_structural_search_tool_missing_pattern_errors(ws):
    from src.agent_tools.structural_search_tools import StructuralSearchTool

    result = asyncio.run(StructuralSearchTool().execute(
        json.dumps({"lang": "python", "path": ws}), {}))
    assert result["exit_code"] == 1
    assert "pattern is required" in result["error"]


# ── real search over this repo's own src/ (reported in the task summary) ─

def test_real_search_over_faustus_src_except_without_log():
    """Not confined to a tmp workspace -- shells out to ast-grep directly
    over this repo's real src/, the way the task's reporting requirement
    asks for (hit count + examples), independent of workspace binding."""
    info = ss.available()
    proc = subprocess.run(
        [info["path"], "run", "--pattern",
         "try:\n    $$$TRY\nexcept Exception:\n    $$$BODY",
         "--lang", "python", "--json=compact", "src"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode in (0, 1)
    hits = json.loads(proc.stdout) if proc.stdout.strip() else []
    assert len(hits) > 0, "expected at least one try/except Exception block in src/"


def test_rewrite_keeps_crlf_files_intact(ws):
    """ast-grep reports byte offsets; a CRLF file read in text mode shifts
    them (seen on Windows, where the fixture files are written with CRLF)."""
    path = os.path.join(ws, "crlf.py")
    with open(path, "wb") as fh:
        fh.write(b"def a():\r\n    foo(1, None)\r\n    foo(2, 'keep')\r\n")
    result = ss.rewrite_apply("foo($A, None)", "foo($A)", "python", path)
    assert result["exit_code"] == 0
    data = open(path, "rb").read()
    assert data == b"def a():\r\n    foo(1)\r\n    foo(2, 'keep')\r\n"
