"""The Objectives section of the project hub (static/js/projects.js): the
list + inline edit UI over /api/projects/{pid}/objectives. The renderers are
pure (kept in a marked, dependency-free region of projects.js) and run in
node; the wiring is pinned at source level."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/projects.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Objectives: pure helpers"
PURE_END = "// ── Objectives: end pure helpers ──"

DATA = {
    "objectives": [
        {"id": "OBJ-1", "title": "Ship the <b>thing</b>", "status": "in_progress", "priority": 1,
         "owner": "user", "notes": "step 1 <script>", "created_at": "2026-09-01T10:00:00",
         "updated_at": "2026-09-02T10:00:00"},
        {"id": "OBJ-2", "title": "Write docs", "status": "open", "priority": 3, "owner": "agent", "notes": ""},
        {"id": "OBJ-3", "title": "Old idea", "status": "dropped", "priority": 2, "owner": "user", "notes": ""},
        {"id": "OBJ-10", "title": "Done one", "status": "done", "priority": 1, "owner": "user", "notes": ""},
        {"id": "OBJ-4", "title": "Blocked task", "status": "blocked", "priority": 2, "owner": "user", "notes": ""},
    ],
    "edges": [{"from": "OBJ-4", "to": "OBJ-1"}, {"from": "OBJ-4", "to": "OBJ-2"}],
    "scores": {"OBJ-1": {"score": 0.62, "hint": "structurally blocking; consider raising priority"},
               "OBJ-2": {"score": 0.1, "hint": None}},
    "log": [
        {"ts": "2026-09-01T10:00:00", "kind": "delta", "actor": "user", "op": "ADD", "id": "OBJ-1",
         "rationale": "kickoff <i>x</i>"},
        {"ts": "2026-09-02T11:00:00", "kind": "conflict", "actor": "agent", "op": "EDIT", "id": "OBJ-1",
         "reason": "human edit wins"},
        {"ts": "2026-09-02T12:00:00", "kind": "evidence", "id": "OBJ-1", "source": "dispatch",
         "ref": "job-1", "confidence": 0.6, "note": "changed cart.py"},
    ],
}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from projects.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/projects.js")], capture_output=True).returncode == 0
    # no inline handlers, no native dialogs (deletion goes through styledConfirm)
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "styledConfirm" in SRC and "data-obj-delete" in SRC
    # the API quartet against the objectives endpoints
    for name in ("listObjectives", "createObjective", "updateObjective", "deleteObjective"):
        assert f"export const {name}" in SRC, name
    assert "/objectives`" in SRC and "/objectives/${encodeURIComponent(oid)}`" in SRC
    # the section is rendered in the hub and refreshed after every mutation
    assert 'id="project-objectives"' in SRC
    assert "renderObjectives(project)" in SRC
    assert "function objectivesSectionHtml" in SRC and "function sortObjectives" in SRC
    assert "function wireObjectives" in SRC and "function patchObjective" in SRC
    # errors land inline, not in a dialog
    assert "data-obj-error" in SRC and "objInlineError" in SRC
    # a clearly delimited CSS block using theme tokens exists
    assert "/* ── Project objectives ──" in CSS
    for selector in (".project-obj-row", ".project-obj-status.is-blocked", ".project-obj-status.is-done",
                     ".project-obj-hint", ".project-obj-add", ".project-obj-log-row"):
        assert selector in CSS, selector


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_sorting_puts_done_and_dropped_last_then_priority_then_id():
    out = _run(f"""
      const DATA = {json.dumps(DATA)};
      console.log(JSON.stringify({{
        order: sortObjectives(DATA.objectives).map(o => o.id),
        empty: sortObjectives([]),
      }}));
    """)
    assert out["order"] == ["OBJ-1", "OBJ-4", "OBJ-2", "OBJ-10", "OBJ-3"]
    assert out["empty"] == []


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_rows_escape_show_deps_hint_and_hide_dropped_by_default():
    out = _run(f"""
      const DATA = {json.dumps(DATA)};
      console.log(JSON.stringify({{
        html: objectivesSectionHtml(DATA, {{}}),
        dropped: objectivesSectionHtml(DATA, {{ showDropped: true }}),
        expanded: objectivesSectionHtml(DATA, {{ expanded: ['OBJ-1'] }}),
      }}));
    """)
    html, dropped, expanded = out["html"], out["dropped"], out["expanded"]
    # heading count excludes dropped; dropped rows hidden behind the toggle
    assert 'class="project-obj-count">4<' in html
    assert 'data-obj-row="OBJ-3"' not in html and 'data-obj-row="OBJ-3"' in dropped
    assert "show dropped" in html and "data-obj-show-dropped" in html
    assert " checked" in dropped and " disabled" in dropped  # dropped row selects are inert
    # titles are escaped
    assert "Ship the &lt;b&gt;thing&lt;/b&gt;" in html and "<b>thing</b>" not in html
    # edges → blocked-by line; scores hint → ⚡ badge with a tooltip
    assert "blocked by OBJ-1, OBJ-2" in html
    assert "⚡" in html and 'title="structurally blocking; consider raising priority"' in html
    # status/priority selects reflect the record; delete is a data-attribute button
    assert 'data-obj-status="OBJ-1"' in html and '<option value="in_progress" selected>in progress</option>' in html
    assert 'data-obj-priority="OBJ-1"' in html and '<option value="1" selected>P1</option>' in html
    assert 'data-obj-delete="OBJ-1"' in html and "onclick=" not in html
    assert 'data-obj-delete="OBJ-3"' not in dropped  # dropped rows are already gone
    # clicking the title expands notes: editable textarea + save button
    assert "data-obj-notes" not in html
    assert 'data-obj-notes="OBJ-1"' in expanded and "step 1 &lt;script&gt;" in expanded
    assert 'data-obj-save-notes="OBJ-1"' in expanded and "<script>" not in expanded
    # activity block: collapsed details with plain log rows, escaped, newest first
    assert "<details" in html and "Activity" in html
    assert "kickoff &lt;i&gt;x&lt;/i&gt;" in html and "<i>x</i>" not in html
    assert "human edit wins" in html and "changed cart.py" in html and "dispatch" in html
    assert html.index("evidence") < html.index("conflict EDIT") < html.index("ADD")


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_empty_state_and_inline_error_keep_the_add_form():
    empty = {"objectives": [], "edges": [], "scores": {}, "log": []}
    out = _run(f"""
      const EMPTY = {json.dumps(empty)};
      console.log(JSON.stringify({{
        empty: objectivesSectionHtml(EMPTY, {{}}),
        error: objectivesSectionHtml(EMPTY, {{ error: 'HTTP 500 <x>' }}),
      }}));
    """)
    assert "No objectives yet" in out["empty"] and "data-obj-add-title" in out["empty"]
    assert "data-obj-add-priority" in out["empty"] and "data-obj-add-deps" in out["empty"]
    assert "data-obj-error hidden>" in out["empty"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-obj-error hidden>" not in out["error"]  # the error is visible
    assert "data-obj-add-title" in out["error"]  # …and the form survives it


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_dep_parsing_and_defensive_payload_unwrapping():
    out = _run("""
      console.log(JSON.stringify({
        deps: parseDepIds('obj-1, 2  OBJ-3, junk,, OBJ-x'),
        none: parseDepIds(''),
        bare: unwrapObjective({ id: 'OBJ-1', title: 't' }).id,
        wrapped: unwrapObjective({ objective: { id: 'OBJ-2', title: 't' } }).id,
        junk: unwrapObjective(null),
        norm: normalizeObjectivesPayload(null),
        nested: normalizeObjectivesPayload({ data: { objectives: [{ id: 'OBJ-1' }], edges: [], scores: {}, log: [] } }).objectives.length,
      }));
    """)
    assert out["deps"] == ["OBJ-1", "OBJ-2", "OBJ-3"]
    assert out["none"] == [] and out["junk"] == {}
    assert out["bare"] == "OBJ-1" and out["wrapped"] == "OBJ-2"
    assert out["norm"] == {"objectives": [], "edges": [], "scores": {}, "log": []}
    assert out["nested"] == 1
