"""Tests for src/plan_tracker.py (P1) and src/agent_tools/plan_tools.py.

Real filesystem persistence throughout: `DATA_DIR` is monkeypatched to a
pytest tmp dir and `src.plan_tracker.PLAN_TRACKER_DIR` is recomputed to
match, so `save`/`load`/`active` exercise real JSON files on disk, not a
mock.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from src import plan_tracker as pt


# ---------------------------------------------------------------------------
# fixtures / synthetic plans
# ---------------------------------------------------------------------------

FAUSTUS_CREATOR_PLAN = """
## WP03 — Dependency drift check

Compare requirements.txt against the installed venv before running anything.

Acceptance:
- Missing packages are listed before the first bash call
- A test covers the Windows-only marker case

Files: src/dependency_drift.py, tests/test_dependency_drift.py

## WP04 — Rewrite policy

depende de WP03. Discourage whole-file rewrites of the same large file.

Acceptance:
- write_file refuses after N rewrites of a >150 line file
- edit_file is suggested instead

Files: src/rewrite_policy.py

## WP05 — Test debt journal

Track pre-existing/exempt tests across turns so they don't stay silently
forgiven.

Criterio de aceptación:
- A test exempt for 3+ turns becomes a high-priority todo

Files: src/test_debt.py
"""

MICROTASK_PLAN = """
### Tarea 05: Separacion de capas

Separar el pipeline en modulos independientes.

Criterios de aceptación:
- [ ] El modulo trace.py no importa mesh.py directamente
- [ ] Existe un test para la separacion

Ficheros: silhouettes/trace.py, silhouettes/mesh.py

### Tarea 06: Editor 2D

after Tarea 05. Construir el editor de capas en /editor.

Criterios de aceptación:
- [ ] La ruta /editor responde 200
- [ ] El viewport dibuja al menos una capa

Ficheros: static/editor/viewport2d.js, templates/editor.html

### Tarea 07: Validacion de malla

Verificar que la malla generada es watertight antes de exportar STL.

Criterios de aceptación:
- [ ] test_real_end_to_end pasa para todas las formas
- [ ] IoU >= 0.98

Ficheros: silhouettes/validation.py
"""

UNSTRUCTURED_TEXT = """
Hey, just wanted to say the demo looked great yesterday. Let's catch up
next week and figure out next steps for the project once everyone is
back from vacation. No rush on anything specific right now, just wanted
to check in and see how things are going on your end before we plan the
next steps together as a team.
""" * 10  # long enough to pass min_chars, but has no headings/lists/checkboxes


def _inline(kind: str, title: str, body: str) -> str:
    return f"=== {kind}: {title} ===\n{body}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    monkeypatch.setattr(pt, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", os.path.join(data_dir, "plan_tracker"))
    yield


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (a) FAUSTUS_CREATOR_PLAN style: "## WP03 — ..." + "Acceptance:"
# ---------------------------------------------------------------------------

def test_parses_wp_style_plan_with_acceptance_label():
    spec = pt.parse_plan("Faustus creator plan", FAUSTUS_CREATOR_PLAN)
    assert len(spec.tasks) == 3
    wp03 = spec.tasks[0]
    assert wp03.key == "WP03"
    assert wp03.id == "t01"
    assert "Missing packages are listed before the first bash call" in wp03.acceptance
    assert "src/dependency_drift.py" in wp03.files
    wp04 = spec.tasks[1]
    assert wp04.key == "WP04"
    assert "WP03" in wp04.depends_on
    wp05 = spec.tasks[2]
    # "Criterio de aceptación:" (singular, accented) is also recognized
    assert any("high-priority todo" in a for a in wp05.acceptance)


# ---------------------------------------------------------------------------
# (b) 24-microtask style: "### Tarea 07: ..." + "Criterios de aceptación"
# ---------------------------------------------------------------------------

def test_parses_numbered_microtask_plan_with_criterios_label():
    spec = pt.parse_plan("Editor de capas", MICROTASK_PLAN)
    assert len(spec.tasks) == 3
    t05, t06, t07 = spec.tasks
    assert t05.key.lower().startswith("tarea 05")
    assert "silhouettes/trace.py" in t05.files
    # checkbox lines count as acceptance items directly
    assert any("no importa mesh.py" in a for a in t05.acceptance)
    assert t06.key.lower().startswith("tarea 06")
    assert any(dep.lower().startswith("tarea 05") for dep in t06.depends_on)
    assert any("IoU >= 0.98" in a for a in t07.acceptance)


# ---------------------------------------------------------------------------
# (c) unstructured text -> 0 tasks -> find_plan_attachment None
# ---------------------------------------------------------------------------

def test_unstructured_text_parses_to_zero_tasks():
    assert len(UNSTRUCTURED_TEXT) >= 3000
    spec = pt.parse_plan("chit chat", UNSTRUCTURED_TEXT)
    assert spec.tasks == []


def test_find_plan_attachment_none_for_unstructured_body():
    msg = "hey, here's some notes\n" + _inline("File", "notes.md", UNSTRUCTURED_TEXT)
    assert pt.find_plan_attachment(msg) is None


def test_find_plan_attachment_none_below_min_chars():
    small_plan = "## Task 1\nDo it.\n## Task 2\nDo it.\n## Task 3\nDo it.\n"
    msg = "here you go\n" + _inline("File", "tiny.md", small_plan)
    assert pt.find_plan_attachment(msg) is None


def test_find_plan_attachment_finds_a_real_plan():
    msg = "here's the plan\n" + _inline("File", "faustus_creator_plan.md", FAUSTUS_CREATOR_PLAN * 20)
    found = pt.find_plan_attachment(msg, min_chars=3000)
    assert found is not None
    title, body = found
    assert title == "faustus_creator_plan.md"


# ---------------------------------------------------------------------------
# (d) idempotency: same body twice -> same hash, seen_count 2, state kept
# ---------------------------------------------------------------------------

def test_upsert_is_idempotent_and_preserves_state():
    scope = "proj-1"
    t1 = pt.upsert_from_attachment(scope, "plan v1", FAUSTUS_CREATOR_PLAN)
    assert t1 is not None
    assert t1["seen_count"] == 1
    h = t1["hash"]

    marked = pt.mark(scope, h, "t01", "done", "ran pytest, 3 passed", turn=2)
    assert marked is not None
    assert marked["state"]["t01"]["status"] == "done"

    t2 = pt.upsert_from_attachment(scope, "plan v1 again", FAUSTUS_CREATOR_PLAN)
    assert t2["hash"] == h
    assert t2["seen_count"] == 2
    # state from the mark() call survives the re-upsert (no re-parse)
    assert t2["state"]["t01"]["status"] == "done"
    assert t2["state"]["t01"]["evidence"] == "ran pytest, 3 passed"


def test_upsert_returns_none_for_unstructured_body():
    assert pt.upsert_from_attachment("proj-x", "notes", UNSTRUCTURED_TEXT) is None


def test_active_returns_most_recently_seen_tracker():
    scope = "proj-active"
    t1 = pt.upsert_from_attachment(scope, "plan a", FAUSTUS_CREATOR_PLAN)
    t2 = pt.upsert_from_attachment(scope, "plan b", MICROTASK_PLAN)
    assert t1["hash"] != t2["hash"]
    active = pt.active(scope)
    assert active["hash"] == t2["hash"]  # most recently upserted


def test_active_none_for_unknown_scope():
    assert pt.active("no-such-scope") is None


# ---------------------------------------------------------------------------
# (e) replace_attachment: authored text + brief + current task only, < 10 KB
#     for a 150 KB synthetic plan
# ---------------------------------------------------------------------------

def test_replace_attachment_strips_a_huge_plan_under_10kb():
    huge_body = FAUSTUS_CREATOR_PLAN * 400  # >> 150 KB
    assert len(huge_body) > 150_000
    scope = "proj-huge"
    tracker = pt.upsert_from_attachment(scope, "huge plan", huge_body)
    assert tracker is not None

    user_msg = "Sigue implementando el plan\n" + _inline("File", "huge plan", huge_body)
    out = pt.replace_attachment(user_msg, tracker, max_task_chars=6000)

    assert "Sigue implementando el plan" in out
    assert out.count(huge_body) == 0
    assert "Active plan" in out
    assert "plan_status" in out
    assert len(out.encode("utf-8")) < 10_000


# ---------------------------------------------------------------------------
# (f) looks_like_execute_request: 8 ES/EN phrases, 4 yes / 4 no
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Sigue implementando el plan",
    "Implementa todo lo posible",
    "Continue implementing this",
    "Finish it",
])
def test_looks_like_execute_request_true_cases(text):
    assert pt.looks_like_execute_request(text) is True


@pytest.mark.parametrize("text", [
    "Aquí tienes el plan para que lo tengas de contexto",
    "Here is the plan for reference, no action needed yet",
    "Does this plan look right to you?",
    "Can you review this and tell me if it makes sense?",
])
def test_looks_like_execute_request_false_cases(text):
    assert pt.looks_like_execute_request(text) is False


# ---------------------------------------------------------------------------
# (g) tools end-to-end, DATA_DIR in tmp
# ---------------------------------------------------------------------------

def _ctx(project_id="proj-tools"):
    return {"project_id": project_id, "turn": 1}


def test_plan_tools_end_to_end():
    from src.agent_tools.plan_tools import (
        PlanStatusTool, PlanTaskTool, PlanDoneTool, PlanSkipTool, PlanNextTool,
    )

    scope = "proj-tools"
    tracker = pt.upsert_from_attachment(scope, "wp plan", FAUSTUS_CREATOR_PLAN)
    assert tracker is not None

    status = _run(PlanStatusTool().execute("{}", _ctx(scope)))
    assert status["exit_code"] == 0
    assert status["progress"] == {"done": 0, "total": 3, "skipped": 0, "pending": 3}

    task = _run(PlanTaskTool().execute(json.dumps({"id": "t01"}), _ctx(scope)))
    assert task["exit_code"] == 0
    assert "Missing packages are listed" in task["output"]

    nxt = _run(PlanNextTool().execute("{}", _ctx(scope)))
    assert nxt["exit_code"] == 0
    assert nxt["task"]["id"] == "t01"

    done = _run(PlanDoneTool().execute(
        json.dumps({"id": "t01", "evidence": "ran pytest -q, 12 passed, 0 failed"}),
        _ctx(scope),
    ))
    assert done["exit_code"] == 0
    assert done["progress"]["done"] == 1

    skip = _run(PlanSkipTool().execute(
        json.dumps({"id": "t02", "reason": "descoped by the user"}),
        _ctx(scope),
    ))
    assert skip["exit_code"] == 0
    assert skip["progress"]["skipped"] == 1

    status2 = _run(PlanStatusTool().execute("{}", _ctx(scope)))
    assert status2["progress"] == {"done": 1, "total": 3, "skipped": 1, "pending": 1}


def test_plan_done_reports_unverified_files():
    from src.agent_tools.plan_tools import PlanDoneTool

    scope = "proj-unverified"
    tracker = pt.upsert_from_attachment(scope, "wp plan", FAUSTUS_CREATOR_PLAN)
    task0 = tracker["tasks"][0]
    assert task0["files"]  # WP03 names src/dependency_drift.py etc.

    ctx = _ctx(scope)
    ctx["ledger_mutations"] = ["some/other/file.py"]
    result = _run(PlanDoneTool().execute(
        json.dumps({"id": task0["id"], "evidence": "checked it manually, looks correct"}),
        ctx,
    ))
    assert result["exit_code"] == 0
    assert set(result.get("unverified_files", [])) == set(task0["files"])


def test_tools_error_when_no_active_plan():
    from src.agent_tools.plan_tools import PlanStatusTool

    result = _run(PlanStatusTool().execute("{}", _ctx("empty-scope")))
    assert result["exit_code"] == 1
    assert "no active plan" in result["error"]


# ---------------------------------------------------------------------------
# (h) plan_done without evidence -> error
# ---------------------------------------------------------------------------

def test_plan_done_requires_evidence():
    from src.agent_tools.plan_tools import PlanDoneTool

    scope = "proj-no-evidence"
    tracker = pt.upsert_from_attachment(scope, "wp plan", FAUSTUS_CREATOR_PLAN)
    task0 = tracker["tasks"][0]

    no_evidence = _run(PlanDoneTool().execute(
        json.dumps({"id": task0["id"]}),
        _ctx(scope),
    ))
    assert no_evidence["exit_code"] == 1
    assert "evidence" in no_evidence["error"]

    too_short = _run(PlanDoneTool().execute(
        json.dumps({"id": task0["id"], "evidence": "done"}),
        _ctx(scope),
    ))
    assert too_short["exit_code"] == 1
    assert "evidence" in too_short["error"]


# ---------------------------------------------------------------------------
# extra: scope_for, progress, current_task, brief determinism
# ---------------------------------------------------------------------------

def test_scope_for_prefers_project_id_over_workspace():
    assert pt.scope_for("proj-42", r"C:\Users\Luis\project") == "proj-42"


def test_scope_for_hashes_normalized_windows_workspace_consistently():
    a = pt.scope_for(None, r"C:\Users\Luis\Silhouettes")
    b = pt.scope_for(None, "C:/Users/Luis/Silhouettes/")
    assert a == b
    assert len(a) == 12


def test_current_task_respects_depends_on():
    spec = pt.parse_plan("plan", FAUSTUS_CREATOR_PLAN)
    tracker = {
        "hash": spec.hash,
        "title": spec.title,
        "tasks": [pt.asdict(t) if hasattr(pt, "asdict") else t.__dict__ for t in spec.tasks],
        "state": {
            "t01": {"status": "pending", "evidence": "", "updated_at": 0, "turn": None},
            "t02": {"status": "pending", "evidence": "", "updated_at": 0, "turn": None},
            "t03": {"status": "pending", "evidence": "", "updated_at": 0, "turn": None},
        },
    }
    # t02 depends on WP03/t01, which is not done yet -> current task is t01
    cur = pt.current_task(tracker)
    assert cur["id"] == "t01"

    tracker["state"]["t01"]["status"] = "done"
    cur2 = pt.current_task(tracker)
    assert cur2["id"] == "t02"


def test_brief_is_deterministic_and_mentions_plan_tools():
    tracker = pt.upsert_from_attachment("proj-brief", "wp plan", FAUSTUS_CREATOR_PLAN)
    b1 = pt.brief(tracker)
    b2 = pt.brief(tracker)
    assert b1 == b2
    assert "plan_status" in b1 and "plan_task" in b1 and "plan_done" in b1
    assert "WP03" in b1


def test_brief_spanish_variant():
    tracker = pt.upsert_from_attachment("proj-brief-es", "wp plan", FAUSTUS_CREATOR_PLAN)
    b = pt.brief(tracker, language="es")
    assert "Plan activo" in b
    assert "no debe volver a pedirse" in b


def test_plan_title_heading_is_not_a_task_and_subtasks_stay_inside_their_task():
    from src import plan_tracker as pt
    body = (
        "# Plan de implementación — servicio de notas\n\nIntro.\n\n"
        "## WP01 — Esqueleto\n\ncuerpo 1\n\n### Detalle interno\n\nmás cuerpo de WP01\n\n"
        "## WP02 — API\n\ncuerpo 2\n\n## WP03 — Página\n\ncuerpo 3\n\n## WP04 — Docs\n\ncuerpo 4\n"
    )
    spec = pt.parse_plan("PLAN_NOTAS.md", body)
    assert [t.key for t in spec.tasks] == ["WP01", "WP02", "WP03", "WP04"]
    assert "más cuerpo de WP01" in spec.tasks[0].text
    assert spec.title.startswith("PLAN_NOTAS.md — Plan de implementación")


def test_reconcile_closes_tasks_from_completed_todos_or_written_files(tmp_path, monkeypatch):
    from src import plan_tracker as pt
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "pt"), raising=False)
    scope = "proj-rec"
    tracker = pt.upsert_from_attachment(scope, "plan.md", FAUSTUS_CREATOR_PLAN)
    assert tracker and pt.progress(tracker)["done"] == 0
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "rewrite_policy.py").write_text("x", encoding="utf-8")
    todos = [{"content": "WP03 — Dependency drift check", "status": "completed"},
             {"content": "WP05 — Test debt journal", "status": "pending"}]
    marked = pt.reconcile(scope, tracker, todos=todos, mutated_paths=["src/rewrite_policy.py"],
                          workspace=str(ws), turn="t1")
    # WP03 by todo, WP04 by its only file existing and written this turn; WP05 stays.
    keys = {t["key"] for t in tracker["tasks"] if t["id"] in marked}
    assert keys == {"WP03", "WP04"}, keys
    assert pt.progress(pt.load(scope, tracker["hash"]))["done"] == 2
    assert pt.current_task(tracker)["key"] == "WP05"
    # Idempotent: nothing new on a second pass.
    assert pt.reconcile(scope, tracker, todos=todos, mutated_paths=["src/rewrite_policy.py"],
                        workspace=str(ws), turn="t1") == []
