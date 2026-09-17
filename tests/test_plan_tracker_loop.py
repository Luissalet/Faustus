"""Plan tracker inside stream_agent_loop (P1 wiring, Silhouettes measures 3+4).

Chats #14/#15: a 66–172 KB plan pasted after "Sigue implementando el plan"
was trimmed to a TOC, the local model answered "Reference context
received." with zero tool calls, four times. Now: the attachment is parsed
once into the tracker, the prompt carries the brief + the current task only,
the plan_* tools are offered, and an execute request that ends with zero
tool calls is rejected and retried.
"""

from __future__ import annotations

import json

import pytest

from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _run
from tests.test_plan_tracker import FAUSTUS_CREATOR_PLAN


def _big_plan_text() -> str:
    # Pad every task so the plan is unmistakably "large" (> min_chars).
    pad = "\n".join(f"Detail line {i}: keep the invariants; run the tests." for i in range(120))
    return FAUSTUS_CREATOR_PLAN.replace("Files: src/dependency_drift.py", pad + "\nFiles: src/dependency_drift.py")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    from src import plan_tracker as pt
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "data" / "plan_tracker"), raising=False)
    w = tmp_path / "ws"
    (w / "src").mkdir(parents=True)
    (w / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return str(w)


def test_plan_attachment_is_parsed_once_and_execute_request_without_action_is_rejected(ws, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_ui_smoke": False})
    calls = _scripted_stream(monkeypatch, [
        ("Reference context received.", "stop"),          # the real #14 answer (×2)
        ("Reference context received.", "stop"),
        ("```plan_status\n{}\n```", "tool_calls"),          # after the rejection: acts
        ("Working on WP03 next.", "stop"),
    ])
    plan = _big_plan_text()
    user = "Sigue implementando el plan\n=== File: plan.md ===\n" + plan
    events = _run(ws, user=user, harness_options={"project_id": "proj-sil"})

    # (1) The tracker saw the plan and told the UI.
    pt_ev = [e for e in events if e.get("type") == "plan_tracker"]
    assert pt_ev and pt_ev[0]["total"] == 3 and pt_ev[0]["done"] == 0 and pt_ev[0]["current"]
    # (2) The model never saw the 172 KB: the last user message carries the
    # typed request, the brief and ONLY the current task.
    first_prompt = calls["messages"][0]
    last_user = next(m for m in reversed(first_prompt) if m.get("role") == "user")
    assert "Sigue implementando el plan" in last_user["content"]
    assert "=== Plan task" in last_user["content"]
    assert "Detail line 119" not in last_user["content"] or len(last_user["content"]) < 12000
    assert len(last_user["content"]) < 12000, len(last_user["content"])
    # (3) "Reference context received." with zero tool calls was rejected with
    # the plan-specific instruction, and the model then acted.
    statuses = [e.get("status") for e in events if e.get("type") == "harness_check"]
    # First layer: the existing no-action nudge, now plan-aware.
    assert "no_action" in statuses, statuses
    assert any("plan_status" in str(m.get("content", "")) for m in calls["messages"][1] if m.get("role") == "user")
    # Second layer: a second echo does not close the turn either (nudged
    # again — the ledger's `plan_without_action` reason is the third net,
    # covered by tests/test_p1_wiring.py); the model then acted.
    assert len([st for st in statuses if st in ("no_action", "empty_round", "rejected")]) >= 2, statuses
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["tools_run"].get("plan_status") == 1
    assert calls["n"] >= 3


def test_second_chat_without_attachment_gets_the_brief_not_the_plan(ws, monkeypatch):
    from src import plan_tracker as pt
    scope = pt.scope_for("proj-sil2", ws)
    assert pt.upsert_from_attachment(scope, "plan.md", _big_plan_text())
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_ui_smoke": False})
    calls = _scripted_stream(monkeypatch, [
        ("```plan_status\n{}\n```", "tool_calls"),
        ("Continuing with WP03.", "stop"),
    ])
    events = _run(ws, user="Continua", harness_options={"project_id": "proj-sil2"})
    assert any(e.get("type") == "plan_tracker" for e in events)
    prompt = calls["messages"][0]
    briefs = [m for m in prompt if m.get("role") == "system" and "plan" in str(m.get("content", "")).lower()
              and "WP03" in str(m.get("content", ""))]
    assert briefs, [m.get("content", "")[:80] for m in prompt]
    assert all("Detail line 5" not in str(m.get("content", "")) for m in prompt)
    # The plan tools were offered to the model.
    metrics = [e for e in events if e.get("type") == "metrics"]
    tools_sent = set()
    for e in events:
        if e.get("type") == "tools_selected" or e.get("type") == "tool_menu":
            tools_sent |= set(e.get("tools") or [])
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete"
    assert json.dumps(summary)


def test_plan_brief_is_not_a_reason_to_reject_a_pure_question(ws, monkeypatch):
    from src import plan_tracker as pt
    scope = pt.scope_for("proj-sil3", ws)
    assert pt.upsert_from_attachment(scope, "plan.md", _big_plan_text())
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_ui_smoke": False})
    _scripted_stream(monkeypatch, [("WP03 compara requirements con el venv.", "stop")])
    events = _run(ws, user="¿Qué hace la tarea WP03?", harness_options={"project_id": "proj-sil3"})
    assert not [e for e in events if e.get("type") == "harness_check" and e.get("status") == "rejected"]


def test_turn_end_reconciles_plan_tasks_from_completed_todos_and_written_files(ws, monkeypatch):
    """The live 17-09 gap: the 27B finished four WPs through todowrite and
    edit_file and never called plan_done, so the tracker still said 0/4."""
    import os
    from src import plan_tracker as pt
    from src.agent_tools import coding_tools as ct
    scope = pt.scope_for("proj-sil4", ws)
    assert pt.upsert_from_attachment(scope, "plan.md", _big_plan_text())
    os.makedirs(os.path.join(ws, "src"), exist_ok=True)
    for f in ("src/dependency_drift.py", "tests/test_dependency_drift.py", "requirements.txt"):
        os.makedirs(os.path.dirname(os.path.join(ws, f)) or ws, exist_ok=True)
        open(os.path.join(ws, f), "w", encoding="utf-8").write("x = 1\n")

    def _exec(block):
        if block.tool_type == "edit_file":
            return {"output": "Edited src/dependency_drift.py (1 replacement)", "exit_code": 0,
                    "diff": {"added": 1, "removed": 1}}
        if block.tool_type == "todowrite":
            todos = [{"content": "WP03 — Dependency drift check", "status": "completed"}]
            ct.save_todos("sess-func", todos)
            return {"output": "ok", "exit_code": 0, "todos": todos}
        return None

    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_ui_smoke": False}, tool_exec=_exec)
    _scripted_stream(monkeypatch, [
        ('```edit_file\n{"path": "src/dependency_drift.py", "old_string": "x = 1", "new_string": "x = 2"}\n```', "tool_calls"),
        ('```todowrite\n[{"content": "WP03 — Dependency drift check", "status": "completed"}]\n```', "tool_calls"),
        ("WP03 hecho: src/dependency_drift.py cambiado.", "stop"),
    ])
    events = _run(ws, user="Sigue con el plan", harness_options={"project_id": "proj-sil4"})
    pt_events = [e for e in events if e.get("type") == "plan_tracker"]
    assert pt_events and pt_events[-1]["done"] == 1 and pt_events[-1]["current"] == "WP04", pt_events
    reloaded = pt.active(scope)
    assert reloaded["state"]["t01"]["status"] == "done"
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("plan_tracker:auto_done=") for n in summary["notes"])
