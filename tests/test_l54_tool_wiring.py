"""Lote 54 — tool wiring for EXEC-05/06, DESK-01, WEB-05/06, EDIT-06, ART-04.

Each of these already had a real, tested library (Lotes 47/48) with no tool
the model could call. This file covers two things:

1. Registration coherence — each new name reaches every place a tool has to
   be listed (`TOOL_TAGS`, `TOOL_HANDLERS`, `FUNCTION_TOOL_SCHEMAS`,
   `tool_capabilities.KNOWN_CAPABILITY_TOOLS`, `tool_index.
   BUILTIN_TOOL_DESCRIPTIONS`, `tool_registry.snapshot()`) — the exact gap
   `tests/test_tool_index_schema_parity.py`/`tests/test_tool_registry.py`
   already guard for every OTHER tool.
2. The wrapper's own logic — args in, the right library call, a sane result
   shape — against the real library functions (rule 7: no HTTP here, so
   direct calls are the equivalent of TestClient; only genuine external
   edges — a real subprocess install, a real SSH connection — are faked, the
   same seam the libraries' OWN tests already fake via `runner=`).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import pytest

import src.agent_tools as agent_tools  # noqa: F401 - resolves circular schema imports first
from src.agent_tools.browser_tools import BrowserExtractTool, CaptureEvidenceTool
from src.agent_tools.code_tools import RenameSymbolTool
from src.agent_tools.desktop_tools import ManageDesktopControlTool
from src.agent_tools.exec_tools import InstallDependenciesTool, ManageScriptsTool
from src.agent_tools.spreadsheet_tools import ManageSpreadsheetTool
from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_registry import snapshot

NEW_TOOLS = (
    "rename_symbol", "install_dependencies", "manage_scripts",
    "manage_desktop_control", "capture_evidence", "browser_extract", "manage_spreadsheet",
)


def _run(coro):
    return asyncio.run(coro)


def _te():
    """`src.tool_execution` as `exec_tools.py` itself sees it right now.

    `src/agent_tools/exec_tools.py` does `from src import tool_execution as
    te` once, at ITS OWN first import. A handful of other test files (e.g.
    ``test_fenced_inline_args.py``) pop ``src.tool_execution`` out of
    ``sys.modules`` at collection time and re-import it, which — same class
    of hazard the comment in ``tests/test_desktop_tools.py`` documents for
    ``importlib.reload`` — leaves a BRAND NEW module object (and inside it a
    brand new ``_active_workspace``/``_active_workspace_roots``
    ``ContextVar`` pair, since those are created by module-level statements
    re-executed on import) in ``sys.modules``, distinct from the one
    ``exec_tools.py`` already captured. `importlib.import_module` here would
    just hand back that same *new* object — no better. What actually has to
    match is whichever module object ``InstallDependenciesTool``/
    ``ManageScriptsTool`` will call into, so fetch it via ``exec_tools``'s
    own ``te`` attribute directly — correct regardless of collection order.
    """
    from src.agent_tools import exec_tools

    return exec_tools.te


@pytest.fixture
def ws(tmp_path):
    te = _te()
    ws_token = te._active_workspace.set(str(tmp_path))
    roots_token = te._active_workspace_roots.set((str(tmp_path),))
    try:
        yield tmp_path
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


# ── 1. Registration coherence ───────────────────────────────────────────

_NATIVE_NAMES = {(e.get("function") or {}).get("name") for e in FUNCTION_TOOL_SCHEMAS}


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_new_tool_reaches_every_registry(name):
    assert name in agent_tools.TOOL_TAGS, f"{name} missing from TOOL_TAGS"
    assert name in agent_tools.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"
    assert name in _NATIVE_NAMES, f"{name} missing from FUNCTION_TOOL_SCHEMAS"
    assert name in KNOWN_CAPABILITY_TOOLS, f"{name} missing an explicit tool_capabilities entry"
    assert name in BUILTIN_TOOL_DESCRIPTIONS, f"{name} missing a tool_index description (unselectable by RAG)"
    assert name in {d.name for d in snapshot()}, f"{name} missing from ToolRegistry.snapshot()"


def test_write_and_install_tools_carry_a_write_or_admin_effect():
    """L54's own instruction: 'las de escritura/instalación exigen
    aprobación' — checked here as the effect classification that makes the
    existing generic post-external-context gate cover them, never a new gate."""
    from src.tool_capabilities import ToolEffect, capabilities_for_tool

    gated = {
        "rename_symbol": ToolEffect.WRITE_WORKSPACE,
        "install_dependencies": ToolEffect.ADMIN_CHANGE,
        "manage_scripts": ToolEffect.EXECUTE_CODE,
        "manage_desktop_control": ToolEffect.ADMIN_CHANGE,
        "manage_spreadsheet": ToolEffect.WRITE_WORKSPACE,
    }
    for name, effect in gated.items():
        caps = capabilities_for_tool(name)
        assert effect in caps.effects, f"{name} should carry {effect}, has {caps.effects}"


# ── 2. install_dependencies (EXEC-05) ───────────────────────────────────

def test_install_dependencies_plan_detects_manager_and_never_runs(ws, monkeypatch):
    (ws / "requirements.txt").write_text("requests\n")
    monkeypatch.setattr(
        _te(), "execute_dependency_install",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("plan must never execute")),
    )
    result = _run(InstallDependenciesTool().execute(
        json.dumps({"action": "plan", "packages": ["requests"]}), {"owner": "alice"}))
    assert result["exit_code"] == 0
    assert result["plan"]["manager"] == "pip"
    assert result["plan"]["packages"] == ["requests"]


def test_install_dependencies_refuses_a_malicious_package_name(ws):
    (ws / "requirements.txt").write_text("")
    result = _run(InstallDependenciesTool().execute(
        json.dumps({"action": "plan", "packages": ["evil; rm -rf /"]}), {"owner": "alice"}))
    assert result["exit_code"] == 1
    assert "error" in result


def test_install_dependencies_install_without_approval_is_refused(ws):
    (ws / "requirements.txt").write_text("")
    result = _run(InstallDependenciesTool().execute(
        json.dumps({"action": "install", "packages": ["requests"]}), {"owner": "alice"}))
    assert result["exit_code"] == 1
    assert "not approved" in result["error"]


def test_install_dependencies_install_with_approval_runs_the_plan(ws, monkeypatch):
    (ws / "requirements.txt").write_text("")
    calls = []

    async def _fake_execute(plan, *, approved, owner, runner=None):
        calls.append((plan.plan_hash, approved, owner))
        return _te().CommandOutcome(
            ok=True, command=("pip", "install", "requests"), stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(_te(), "execute_dependency_install", _fake_execute)
    result = _run(InstallDependenciesTool().execute(
        json.dumps({"action": "install", "packages": ["requests"], "approved": True}), {"owner": "alice"}))
    assert result["exit_code"] == 0
    assert calls and calls[0][1] is True and calls[0][2] == "alice"


# ── 3. manage_scripts (EXEC-06) ──────────────────────────────────────────

def test_manage_scripts_save_versions_never_overwrites(ws, monkeypatch):
    monkeypatch.setattr(_te(), "DATA_DIR", str(ws))
    tool = ManageScriptsTool()
    first = _run(tool.execute(json.dumps({
        "action": "save", "name": "greet", "command_template": "echo {name}", "params": ["name"],
    }), {}))
    second = _run(tool.execute(json.dumps({
        "action": "save", "name": "greet", "command_template": "echo hi {name}", "params": ["name"],
    }), {}))
    assert first["script"]["version"] == 1
    assert second["script"]["version"] == 2


def test_manage_scripts_run_renders_and_executes_for_real(ws, monkeypatch):
    """No mock at the subprocess boundary: `echo` is a real, harmless command,
    the same trust level the library's own tests give it."""
    monkeypatch.setattr(_te(), "DATA_DIR", str(ws))
    tool = ManageScriptsTool()
    _run(tool.execute(json.dumps({
        "action": "save", "name": "echoer", "command_template": "echo {msg}", "params": ["msg"],
    }), {}))
    result = _run(tool.execute(json.dumps({
        "action": "run", "name": "echoer", "values": {"msg": "hello-l54"},
    }), {}))
    assert result["exit_code"] == 0
    assert "hello-l54" in result["stdout"]


def test_manage_scripts_run_remote_refuses_a_mismatched_fingerprint(ws, monkeypatch):
    monkeypatch.setattr(_te(), "DATA_DIR", str(ws))
    te = _te()
    te.reset_ssh_registry()
    te.pair_ssh_target("prod", "prod.example.com", "AA:BB:CC")
    tool = ManageScriptsTool()
    _run(tool.execute(json.dumps({
        "action": "save", "name": "uptime_check", "command_template": "uptime", "params": [],
    }), {}))
    result = _run(tool.execute(json.dumps({
        "action": "run_remote", "name": "uptime_check", "values": {},
        "alias": "prod", "host": "prod.evil.example.com", "fingerprint": "AA:BB:CC",
    }), {}))
    assert result["exit_code"] == 1
    assert "not" in result["error"] or "does not" in result["error"] or "not inherit" in result["error"]
    te.reset_ssh_registry()


# ── 4. manage_desktop_control (DESK-01) ─────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_desk01():
    from src.desktop_control_session import reset_desk01_state
    reset_desk01_state()
    yield
    reset_desk01_state()


def test_manage_desktop_control_allowlist_round_trip():
    tool = ManageDesktopControlTool()
    ctx = {"session_id": "s1"}
    empty = _run(tool.execute(json.dumps({"action": "get_allowlist"}), ctx))
    assert empty["allowlist"] == []
    _run(tool.execute(json.dumps({"action": "set_allowlist", "titles": ["Notepad"]}), ctx))
    got = _run(tool.execute(json.dumps({"action": "get_allowlist"}), ctx))
    assert got["allowlist"] == ["Notepad"]
    _run(tool.execute(json.dumps({"action": "clear_allowlist"}), ctx))
    assert _run(tool.execute(json.dumps({"action": "get_allowlist"}), ctx))["allowlist"] == []


def test_manage_desktop_control_audit_toggle_and_log():
    from src.desktop_control_session import audit_enabled, record_action

    tool = ManageDesktopControlTool()
    ctx = {"session_id": "s2"}
    on = _run(tool.execute(json.dumps({"action": "enable_audit"}), ctx))
    assert on["audit_enabled"] is True and audit_enabled("s2") is True
    record_action("s2", "desktop_click", before_hash="a", after_hash="b")
    log = _run(tool.execute(json.dumps({"action": "audit_log"}), ctx))
    assert log["entries"] and log["entries"][0]["tool"] == "desktop_click"
    off = _run(tool.execute(json.dumps({"action": "disable_audit"}), ctx))
    assert off["audit_enabled"] is False and audit_enabled("s2") is False


def test_manage_desktop_control_rejects_an_unknown_action():
    result = _run(ManageDesktopControlTool().execute(json.dumps({"action": "nope"}), {}))
    assert result["exit_code"] == 1 and "error" in result


# ── 5. capture_evidence (WEB-05) ─────────────────────────────────────────

_TINY_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-bytes-for-a-test").decode()


def test_capture_evidence_build_records_resolution_scale_and_hash():
    result = _run(CaptureEvidenceTool().execute(json.dumps({
        "action": "build", "url": "https://example.com/page", "width": 1280, "height": 800,
        "scale": 2.0, "image_b64": _TINY_PNG, "dom_hash": "dom1",
    }), {"owner": "alice"}))
    assert result["exit_code"] == 0
    assert result["capture"]["resolution"] == {"width": 1280, "height": 800}
    assert result["capture"]["scale"] == 2.0
    assert result["capture"]["image_sha256"] == hashlib.sha256(base64.b64decode(_TINY_PNG)).hexdigest()
    assert result["evidence"]["source_type"] == "media"


def test_capture_evidence_compare_and_check_stale():
    tool = CaptureEvidenceTool()
    before = _run(tool.execute(json.dumps({
        "action": "build", "url": "https://example.com/a", "width": 100, "height": 100,
        "image_b64": _TINY_PNG, "dom_hash": "dom1",
    }), {}))["capture"]
    after = _run(tool.execute(json.dumps({
        "action": "build", "url": "https://example.com/a", "width": 100, "height": 100,
        "image_b64": base64.b64encode(b"different-bytes").decode(), "dom_hash": "dom2",
    }), {}))["capture"]
    diff = _run(tool.execute(json.dumps({"action": "compare", "before": before, "after": after}), {}))
    assert diff["same_page"] is True and diff["image_changed"] is True and diff["dom_changed"] is True

    # WEB-05's own acceptance criterion: a capture of a DIFFERENT page is stale.
    stale = _run(tool.execute(json.dumps({
        "action": "check_stale", "capture": before, "current_url": "https://example.com/OTHER",
    }), {}))
    assert stale["stale"] is True and "re-inspect" in stale["reason"]

    fresh = _run(tool.execute(json.dumps({
        "action": "check_stale", "capture": before, "current_url": "https://example.com/a",
        "current_dom_hash": "dom1",
    }), {}))
    assert fresh["stale"] is False and fresh["reason"] is None


def test_capture_evidence_build_without_image_is_a_clean_error():
    result = _run(CaptureEvidenceTool().execute(json.dumps({"action": "build", "url": "x", "width": 1, "height": 1}), {}))
    assert result["exit_code"] == 1 and "error" in result


# ── 6. browser_extract (WEB-06) ──────────────────────────────────────────

def test_browser_extract_paginate_stops_at_max_pages_and_says_why():
    pages = [{"items": [{"a": 1}], "has_next": True} for _ in range(5)]
    result = _run(BrowserExtractTool().execute(json.dumps({
        "action": "paginate", "pages": pages, "max_pages": 2, "max_items": 999,
    }), {}))
    assert result["exit_code"] == 0
    assert result["pages_fetched"] == 2 and result["truncated"] is True and result["reason"] == "max_pages"


def test_browser_extract_paginate_exhausted_is_not_truncated():
    pages = [{"items": [{"a": 1}], "has_next": False}]
    result = _run(BrowserExtractTool().execute(json.dumps({"action": "paginate", "pages": pages}), {}))
    assert result["truncated"] is False and result["reason"] == "exhausted"


def test_browser_extract_detect_restricted_access():
    r = _run(BrowserExtractTool().execute(
        json.dumps({"action": "detect_restricted_access", "page_text": "Error: Access Denied"}), {}))
    assert r["restricted"] is True
    r2 = _run(BrowserExtractTool().execute(
        json.dumps({"action": "detect_restricted_access", "page_text": "Welcome to the page"}), {}))
    assert r2["restricted"] is False


def test_browser_extract_select_download_target_stays_in_sandbox(ws):
    result = _run(BrowserExtractTool().execute(json.dumps({
        "action": "select_download_target", "sandbox_root": str(ws), "filename": "../../etc/passwd",
    }), {}))
    assert result["exit_code"] == 0
    assert os.path.dirname(result["path"]) == os.path.realpath(str(ws))
    assert os.path.basename(result["path"]) == "passwd"


def test_browser_extract_verify_download_flags_a_short_transfer(ws):
    path = ws / "file.bin"
    path.write_bytes(b"partial")
    result = _run(BrowserExtractTool().execute(json.dumps({
        "action": "verify_download", "path": str(path), "expected_total_bytes": 1000,
    }), {}))
    assert result["complete"] is False
    assert result["exit_code"] == 1


def test_browser_extract_verify_download_complete_match():
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello world")
        path = f.name
    try:
        digest = hashlib.sha256(b"hello world").hexdigest()
        result = _run(BrowserExtractTool().execute(json.dumps({
            "action": "verify_download", "path": path, "expected_sha256": digest, "expected_bytes": 11,
        }), {}))
        assert result["complete"] is True and result["exit_code"] == 0
    finally:
        os.unlink(path)


def test_browser_extract_check_login_reuse_uses_the_web03_policy(tmp_path):
    from src import browser_sessions

    browser_sessions.open_session("alice", "task1", allow_logins=True, base_dir=str(tmp_path))
    result = _run(BrowserExtractTool().execute(
        json.dumps({"action": "check_login_reuse", "task_id": "task1"}), {"owner": "alice"}))
    assert result["allowed"] is True
    browser_sessions.close_session("alice", "task1")


def test_browser_extract_check_login_reuse_no_session_is_a_clean_error():
    result = _run(BrowserExtractTool().execute(
        json.dumps({"action": "check_login_reuse", "task_id": "no-such-task"}), {"owner": "nobody"}))
    assert result["exit_code"] == 1 and "error" in result


# ── 7. manage_spreadsheet (ART-04) ───────────────────────────────────────

def test_manage_spreadsheet_import_csv_classifies_and_export_blocks_formulas():
    tool = ManageSpreadsheetTool()
    imported = _run(tool.execute(json.dumps({"action": "import_csv", "text": "id,total\n007,3.5\n"}), {}))
    assert imported["exit_code"] == 0
    assert imported["rows"] == [["id", "total"], ["007", 3.5]]
    assert imported["types"][1][0] == "text"  # a leading-zero ID stays text, never scientific notation

    exported = _run(tool.execute(json.dumps({
        "action": "export_csv", "rows": [["=cmd|'/c calc'!A1", "safe"]],
    }), {}))
    assert exported["csv"].startswith("'=cmd")  # formula-injection neutralized


def test_manage_spreadsheet_write_range_then_read_workbook_flags_stale_formula(ws):
    openpyxl = pytest.importorskip("openpyxl")
    path = ws / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.save(str(path))

    tool = ManageSpreadsheetTool()
    written = _run(tool.execute(json.dumps({
        "action": "write_range", "path": str(path), "sheet_name": "Sheet", "start_cell": "B1",
        "values": [["=A1+1"]],
    }), {}))
    assert written["exit_code"] == 0

    preview = _run(tool.execute(json.dumps({"action": "read_workbook", "path": str(path)}), {}))
    assert preview["exit_code"] == 0
    assert preview["sheets"][0]["needs_recalculation"] is True  # openpyxl never computes formulas


def test_manage_spreadsheet_unknown_action_is_a_clean_error():
    result = _run(ManageSpreadsheetTool().execute(json.dumps({"action": "delete_everything"}), {}))
    assert result["exit_code"] == 1 and "error" in result


# ── 8. rename_symbol (EDIT-06) ───────────────────────────────────────────

def test_rename_symbol_plan_then_apply_touches_only_real_references(ws):
    (ws / "mod.py").write_text(
        "def old_func():\n    return 1\n\n\ndef caller():\n    return old_func() + 1\n"
    )
    (ws / "unrelated.txt").write_text("old_func is mentioned here as prose, not code\n")
    from src import code_index
    code_index.refresh(str(ws))

    tool = RenameSymbolTool()
    plan = _run(tool.execute(json.dumps({
        "old_name": "old_func", "new_name": "new_func", "action": "plan", "path": str(ws),
    }), {}))
    assert plan["exit_code"] == 0 and plan["indexed"] is True
    assert "mod.py" in plan["files"]

    applied = _run(tool.execute(json.dumps({
        "old_name": "old_func", "new_name": "new_func", "action": "apply", "path": str(ws),
    }), {"owner": "alice", "session_id": "s1"}))
    assert applied["exit_code"] == 0 and applied["applied"] is True
    new_text = (ws / "mod.py").read_text()
    assert "new_func" in new_text and "old_func" not in new_text
    # the unrelated prose mention must never have been touched
    assert (ws / "unrelated.txt").read_text() == "old_func is mentioned here as prose, not code\n"


def test_rename_symbol_refuses_an_unindexed_name(ws):
    (ws / "mod.py").write_text("x = 1\n")
    from src import code_index
    code_index.refresh(str(ws))
    result = _run(RenameSymbolTool().execute(json.dumps({
        "old_name": "totally_absent_symbol_zzz", "new_name": "whatever", "action": "plan", "path": str(ws),
    }), {}))
    assert result["exit_code"] == 1
    assert result["indexed"] is False
