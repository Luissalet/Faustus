"""Lot D — src/fix_memory.py, src/agent_tools/fix_memory_tools.py,
routes/fix_memory_routes.py.

Recording from synthetic tool_events (diff-based and command-based file
detection, error signature extraction, skip-when-no-files, deterministic
summary fallback), recall scoring (query overlap, file/error boosts, k),
prompt block budget, rotation policy, forget, the tool's JSON output, and
the HTTP routes.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import fix_memory


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(fix_memory, "DATA_DIR", str(data_dir), raising=False)
    # Never actually call an LLM in tests: no endpoint resolves.
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: (None, None, None),
        raising=False,
    )
    return data_dir


def _diff_tool_event(path: str, symbol: str = "handle_thing") -> dict:
    diff = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,3 +1,4 @@ def {symbol}():\n"
        f"+    fix_applied = True\n"
    )
    return {"tool": "edit_file", "command": path, "output": "ok", "diff": diff}


def _command_only_write_event(path: str) -> dict:
    return {"tool": "write_file", "command": json.dumps({"path": path, "content": "x"}), "output": "ok"}


def _bash_error_event(text: str) -> dict:
    return {"tool": "bash", "command": "pytest -q", "output": text}


TRACEBACK_TEXT = (
    "Traceback (most recent call last):\n"
    '  File "/home/user/app/mod.py", line 42, in run\n'
    "    raise ValueError(\"bad thing 12\")\n"
    "ValueError: bad thing 12\n"
)


# ── recording: file/symbol/error extraction ─────────────────────────────

def test_record_from_turn_extracts_files_and_symbols_from_diff(isolated_store):
    tool_events = [_diff_tool_event("src/app/mod.py", symbol="run")]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="fix the crash in run()",
        tool_events=tool_events, harness_meta={"tests": {"ok": True, "summary": "3 passed"}},
    ))
    assert entry is not None
    assert entry["files"] == ["src/app/mod.py"]
    assert entry["symbols"] == ["run"]
    assert entry["outcome"] == "fixed"


def test_record_from_turn_falls_back_to_command_path_when_no_diff(isolated_store):
    tool_events = [_command_only_write_event("src/new_module.py")]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="create a new module",
        tool_events=tool_events, harness_meta=None,
    ))
    assert entry is not None
    assert entry["files"] == ["src/new_module.py"]
    assert entry["symbols"] == []


def test_record_from_turn_extracts_normalized_error_signature(isolated_store):
    tool_events = [_diff_tool_event("src/app/mod.py"), _bash_error_event(TRACEBACK_TEXT)]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="fix ValueError",
        tool_events=tool_events, harness_meta=None,
    ))
    assert entry is not None
    assert entry["errors"], "expected at least one normalized error signature"
    sig = entry["errors"][0]
    assert "ValueError" in sig
    assert "PATH" in sig or "/home" not in sig
    assert "42" not in sig  # line number masked


def test_record_from_turn_skips_when_no_files_changed(isolated_store):
    tool_events = [_bash_error_event("just some shell output, no file tools")]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="just looked around",
        tool_events=tool_events, harness_meta=None,
    ))
    assert entry is None


def test_record_from_turn_returns_none_without_owner(isolated_store):
    tool_events = [_diff_tool_event("a.py")]
    entry = asyncio.run(fix_memory.record_from_turn(
        None, project_key="proj-a", user_message="x", tool_events=tool_events,
    ))
    assert entry is None


def test_record_from_turn_respects_disabled_setting(isolated_store, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "fix_memory_enabled" else default,
    )
    tool_events = [_diff_tool_event("a.py")]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="x", tool_events=tool_events,
    ))
    assert entry is None


# ── deterministic summary + outcome derivation ──────────────────────────

def test_deterministic_summary_used_when_no_model_endpoint(isolated_store):
    tool_events = [_diff_tool_event("src/app/mod.py")]
    entry = asyncio.run(fix_memory.record_from_turn(
        "alice", project_key="proj-a", user_message="fix it",
        tool_events=tool_events, harness_meta={"tests": {"ok": True, "summary": "3 passed"}},
    ))
    assert "mod.py" in entry["solution"]
    assert "3 passed" in entry["solution"]


def test_outcome_unverified_without_tests():
    assert fix_memory._derive_outcome(None, None) == "unverified"


def test_outcome_fixed_when_tests_ok():
    tests = {"ok": True, "summary": "5 passed"}
    assert fix_memory._derive_outcome({"stop_reason": "complete"}, tests) == "fixed"


def test_outcome_partial_when_tests_fail():
    tests = {"ok": False, "summary": "1 failed"}
    assert fix_memory._derive_outcome({"stop_reason": "complete"}, tests) == "partial"


def test_outcome_unverified_when_gate_downgraded():
    tests = {"ok": True, "summary": "5 passed"}
    assert fix_memory._derive_outcome({"stop_reason": "complete_unverified"}, tests) == "unverified"


# ── recall scoring ───────────────────────────────────────────────────────

def _record(owner, project, task, files, errors=None, ts_offset=0.0):
    import time as _time
    entry = {
        "id": f"fix-{task[:8]}-{ts_offset}",
        "ts": _time.time() + ts_offset,
        "project": project,
        "task": task,
        "files": files,
        "symbols": [],
        "errors": errors or [],
        "tests": None,
        "outcome": "fixed",
        "solution": f"solution for {task}",
        "tags": [],
    }
    fix_memory._append_entry(owner, project, entry)
    return entry


def test_recall_scores_query_overlap_highest_first(isolated_store):
    _record("bob", "proj-b", "fix the login timeout bug", ["auth.py"])
    _record("bob", "proj-b", "add a new dashboard widget", ["ui.py"], ts_offset=1.0)
    results = fix_memory.recall("bob", "proj-b", "login timeout is broken again", k=5)
    assert results
    assert results[0]["task"] == "fix the login timeout bug"


def test_recall_boosts_shared_files(isolated_store):
    _record("bob", "proj-b", "unrelated task one", ["a.py"])
    _record("bob", "proj-b", "unrelated task two", ["shared_module.py"], ts_offset=1.0)
    results = fix_memory.recall("bob", "proj-b", "totally different wording",
                                 files=["shared_module.py"], k=5)
    assert results[0]["files"] == ["shared_module.py"]


def test_recall_boosts_matching_error_signature(isolated_store):
    sig = fix_memory.normalize_error("ValueError: bad thing 99")
    _record("bob", "proj-b", "some other fix", ["x.py"])
    _record("bob", "proj-b", "fixed a value error", ["y.py"], errors=[sig], ts_offset=1.0)
    results = fix_memory.recall("bob", "proj-b", "different words entirely",
                                 errors=["ValueError: bad thing 5"], k=5)
    assert results[0]["errors"] == [sig]


def test_recall_respects_k_limit(isolated_store):
    for i in range(10):
        _record("bob", "proj-b", f"task number {i}", ["f.py"], ts_offset=float(i))
    results = fix_memory.recall("bob", "proj-b", "task", k=3)
    assert len(results) == 3


def test_recall_empty_project_returns_empty(isolated_store):
    assert fix_memory.recall("bob", "no-such-project", "anything") == []


def test_recall_returns_empty_without_owner(isolated_store):
    assert fix_memory.recall(None, "proj-b", "anything") == []


# ── prompt block ─────────────────────────────────────────────────────────

def test_prompt_block_empty_for_no_entries():
    assert fix_memory.prompt_block([]) == ""


def test_prompt_block_lists_entries_and_files():
    entries = [{"task": "fix login bug", "solution": "added retry logic", "files": ["auth.py"]}]
    block = fix_memory.prompt_block(entries)
    assert "Past fixes in this project:" in block
    assert "fix login bug" in block
    assert "auth.py" in block


def test_prompt_block_cuts_to_budget():
    entries = [
        {"task": f"task {i} " + ("x" * 100), "solution": "y" * 100, "files": []}
        for i in range(20)
    ]
    block = fix_memory.prompt_block(entries, budget_tokens=50)
    lines = block.splitlines()
    # header + at least one entry, but not all 20
    assert 1 < len(lines) < 21


# ── rotation ─────────────────────────────────────────────────────────────

def test_rotation_drops_unverified_first(isolated_store):
    entries = []
    for i in range(fix_memory.MAX_ENTRIES_PER_PROJECT + 5):
        outcome = "unverified" if i < 10 else "fixed"
        entries.append({
            "id": f"fix-{i}", "ts": float(i), "project": "proj-r", "task": f"t{i}",
            "files": ["f.py"], "symbols": [], "errors": [], "tests": None,
            "outcome": outcome, "solution": "s", "tags": [],
        })
    rotated = fix_memory._rotate(entries)
    assert len(rotated) == fix_memory.MAX_ENTRIES_PER_PROJECT
    kept_ids = {e["id"] for e in rotated}
    # the first 5 unverified (oldest) entries should be the ones dropped
    for i in range(5):
        assert f"fix-{i}" not in kept_ids


def test_rotation_noop_under_cap():
    entries = [{"id": "a", "ts": 1.0, "outcome": "fixed"}]
    assert fix_memory._rotate(entries) == entries


# ── forget ───────────────────────────────────────────────────────────────

def test_forget_removes_entry_across_projects(isolated_store):
    entry = _record("carol", "proj-c", "some fix", ["a.py"])
    assert fix_memory.forget("carol", entry["id"]) is True
    assert fix_memory.recall("carol", "proj-c", "some fix") == []


def test_forget_returns_false_for_unknown_id(isolated_store):
    assert fix_memory.forget("carol", "not-a-real-id") is False


def test_forget_returns_false_without_owner(isolated_store):
    assert fix_memory.forget(None, "anything") is False


# ── stats ────────────────────────────────────────────────────────────────

def test_stats_counts_by_outcome(isolated_store):
    _record("dave", "proj-d", "task a", ["a.py"])
    e2 = {
        "id": "fix-other", "ts": 2.0, "project": "proj-d", "task": "task b",
        "files": ["b.py"], "symbols": [], "errors": [], "tests": None,
        "outcome": "unverified", "solution": "s", "tags": ["py", "unverified"],
    }
    fix_memory._append_entry("dave", "proj-d", e2)
    result = fix_memory.stats("dave", "proj-d")
    assert result["total"] == 2
    assert result["by_outcome"]["fixed"] == 1
    assert result["by_outcome"]["unverified"] == 1


# ── tool ─────────────────────────────────────────────────────────────────

def test_recall_fixes_tool_returns_json_serializable_result(isolated_store):
    from src.agent_tools.fix_memory_tools import RecallFixesTool

    _record("erin", "/workspace/proj-e", "fix the parser crash", ["parser.py"])
    tool = RecallFixesTool()
    result = asyncio.run(tool.execute(
        json.dumps({"query": "parser crash", "k": 3}),
        {"owner": "erin", "workspace": "/workspace/proj-e"},
    ))
    json.dumps(result)  # must be JSON-serializable
    assert result["count"] == 1
    assert result["fixes"][0]["task"] == "fix the parser crash"


def test_recall_fixes_tool_handles_no_results(isolated_store):
    from src.agent_tools.fix_memory_tools import RecallFixesTool

    tool = RecallFixesTool()
    result = asyncio.run(tool.execute(json.dumps({"query": "anything"}), {"owner": "erin"}))
    assert result["count"] == 0


# ── routes ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client(isolated_store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import fix_memory_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(
        fix_memory_routes, "get_current_user",
        lambda request: request.headers.get("x-test-owner") or None,
    )
    app = FastAPI()
    app.include_router(fix_memory_routes.setup_fix_memory_routes())
    return TestClient(app)


def test_route_list_and_stats_smoke(client):
    headers = {"x-test-owner": "frank"}
    _record("frank", "/ws/proj-f", "fix the button", ["ui.py"])
    resp = client.get("/api/fix-memory", params={"workspace": "/ws/proj-f", "q": "button"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1

    resp2 = client.get("/api/fix-memory/stats", params={"workspace": "/ws/proj-f"}, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1


def test_route_delete_smoke(client):
    headers = {"x-test-owner": "frank"}
    entry = _record("frank", "/ws/proj-f", "fix the other button", ["ui2.py"])
    resp = client.delete(f"/api/fix-memory/{entry['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    resp2 = client.delete("/api/fix-memory/not-a-real-id", headers=headers)
    assert resp2.status_code == 404
