"""The Learned rules section of the Brain page (static/js/memory.js): the
memory-engine list + feedback/curator UI over /api/memory-engine/*. The
renderers are pure (kept in a marked, dependency-free region of memory.js) and
run in node; the wiring is pinned at source level."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/memory.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Learned rules: pure helpers"
PURE_END = "// ── Learned rules: end pure helpers ──"

ITEMS = [
    {"id": "a1", "text": "Run <b>the tests</b> before committing", "level": "procedural",
     "category": "workflow", "trust_class": "human_explicit", "status": "active",
     "maturity": "proven", "effective_score": 0.82, "harmful_ratio": 0.0, "project": "faustus",
     "created_at": "2026-09-01T10:00:00", "updated_at": "2026-09-02T10:00:00"},
    {"id": "a2", "text": "Prefer sqlite WAL", "level": "semantic", "category": "db",
     "trust_class": "agent_validated", "status": "active", "maturity": "established",
     "effective_score": 0.41, "harmful_ratio": 0.1, "project": "faustus"},
    {"id": "b1", "text": "AVOID: monkeypatch the router <script>", "level": "procedural",
     "category": "", "trust_class": "agent_assertion", "status": "anti_pattern",
     "maturity": "candidate", "effective_score": 0.05, "harmful_ratio": 0.75, "project": ""},
    {"id": "c1", "text": "Old habit", "level": "episodic", "trust_class": "legacy_import",
     "status": "deprecated", "maturity": "deprecated", "effective_score": 0.01,
     "harmful_ratio": 0.0, "project": ""},
    {"id": "a0", "text": "Same score, earlier id", "level": "working", "status": "active",
     "maturity": "candidate", "effective_score": 0.41, "harmful_ratio": 0},
]

REPORT = {"deduped": 2, "inverted": 1, "promoted": 3, "demoted": 0, "pruned": 4, "total_active": 11}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from memory.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/memory.js")],
                          capture_output=True).returncode == 0
    # no inline handlers, no native dialogs (deletion goes through styledConfirm)
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "styledConfirm" in SRC and "data-lr-delete" in SRC
    # the memory-engine endpoints, all under the one request helper
    assert "/api/memory-engine" in SRC
    for path in ("'/items?limit=500'", "'/items'", "/feedback`", "'/curate'"):
        assert path in SRC, path
    assert "method: 'DELETE'" in SRC
    # exported entry points + the pure helpers the tests below drive
    assert "export async function loadLearnedRules" in SRC
    assert "export function renderLearnedRules" in SRC
    for fn in ("learnedRulesSectionHtml", "learnedRuleRowHtml", "sortLearnedRules",
               "filterLearnedRules", "normalizeLearnedItems", "unwrapLearnedItem",
               "curatorReportHtml", "learnedScoreHtml"):
        assert f"function {fn}" in SRC, fn
    # the section mounts itself into the Brain modal (index.html is untouched)
    assert "_ensureLearnedRulesUi" in SRC and "learned-rules-section" in SRC
    assert "data-memory-tab" in SRC and "'learned'" in SRC
    assert "loadLearnedRules()" in SRC
    # delegated listeners on the host, not per-row handlers
    assert "_wireLearnedRules" in SRC and "host.addEventListener('click'" in SRC
    assert "host.addEventListener('submit'" in SRC
    # errors land inline, not in a dialog
    assert "data-lr-error" in SRC and "_lrInlineError" in SRC
    # a clearly delimited CSS block using theme tokens exists
    assert "/* ── Learned rules ──" in CSS
    for selector in (".learned-rules-section", ".lr-row.is-anti", ".lr-avoid-badge",
                     ".lr-score-fill", ".lr-filter-chip.active", ".lr-report", ".lr-add-input"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Learned rules ──", 1)[1]
    assert "#" not in block.split("*/", 1)[1], "hardcoded hex colour in the Learned rules block"


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_sorting_puts_anti_patterns_then_deprecated_last_and_ties_break_on_id():
    out = _run(f"""
      const ITEMS = {json.dumps(ITEMS)};
      console.log(JSON.stringify({{
        order: sortLearnedRules(ITEMS).map(i => i.id),
        empty: sortLearnedRules([]),
        junkStatus: sortLearnedRules([{{id:'z',status:'weird',effective_score:9}},
                                      {{id:'y',status:'active',effective_score:0}}]).map(i => i.id),
      }}));
    """)
    # active (score desc, id asc on ties) → anti_pattern → deprecated
    assert out["order"] == ["a1", "a0", "a2", "b1", "c1"]
    assert out["empty"] == []
    assert out["junkStatus"] == ["y", "z"]  # unknown status sorts after the known ones


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_filter_selects_active_and_anti_patterns():
    out = _run(f"""
      const ITEMS = {json.dumps(ITEMS)};
      console.log(JSON.stringify({{
        all: filterLearnedRules(ITEMS, 'all').map(i => i.id),
        active: filterLearnedRules(ITEMS, 'active').map(i => i.id),
        anti: filterLearnedRules(ITEMS, 'anti').map(i => i.id),
        junk: filterLearnedRules(ITEMS, 'nonsense').map(i => i.id),
      }}));
    """)
    assert out["all"] == ["a1", "a2", "b1", "c1", "a0"]
    assert out["active"] == ["a1", "a2", "a0"]
    assert out["anti"] == ["b1"]
    assert out["junk"] == out["all"]  # unknown filter shows everything


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_rows_escape_show_chips_score_and_flag_anti_patterns():
    out = _run(f"""
      const ITEMS = {json.dumps(ITEMS)};
      console.log(JSON.stringify({{
        html: learnedRulesSectionHtml(ITEMS, {{}}),
        anti: learnedRulesSectionHtml(ITEMS, {{ filter: 'anti' }}),
        score: learnedScoreHtml(0.82),
        clamped: learnedScoreHtml(-3) + learnedScoreHtml(7) + learnedScoreHtml('nope'),
      }}));
    """)
    html = out["html"]
    # counts in the filter chips
    assert "All (5)" in html and "Active (3)" in html and "Anti-patterns (1)" in html
    # text is escaped
    assert "Run &lt;b&gt;the tests&lt;/b&gt; before committing" in html
    assert "<b>the tests</b>" not in html and "<script>" not in html
    # chips: level + maturity + trust class
    assert 'class="lr-chip lr-level lr-level-procedural">procedural<' in html
    assert 'class="lr-chip lr-maturity lr-maturity-proven">proven<' in html
    assert "human_explicit" in html and "agent_validated" in html
    # score: number + bar fill, clamped to [0,1]
    assert "0.82" in out["score"] and 'style="width:82%"' in out["score"]
    assert 'style="width:0%"' in out["clamped"] and 'style="width:100%"' in out["clamped"]
    assert "0.00" in out["clamped"]  # non-numeric degrades to zero, never NaN
    assert "NaN" not in out["clamped"]
    # anti-pattern row: highlighted + AVOID badge; deprecated row dimmed
    assert 'class="lr-row is-anti" data-lr-row="b1"' in html
    assert '<span class="lr-avoid-badge">AVOID</span>' in html
    assert html.count("lr-avoid-badge") == 1 and out["anti"].count("lr-avoid-badge") == 1
    assert "is-deprecated" in html
    assert "75% harmful" in html
    # per-row actions are data attributes on delegated buttons
    assert 'data-lr-feedback="a1" data-lr-kind="helpful"' in html
    assert 'data-lr-feedback="a1" data-lr-kind="harmful"' in html
    assert 'data-lr-delete="a1"' in html and "onclick=" not in html
    assert "👍" in html and "👎" in html
    # curator button + add form
    assert "data-lr-curate" in html and "Run curator" in html
    assert "data-lr-add-text" in html and "data-lr-add-level" in html
    assert '<option value="procedural" selected>procedural</option>' in html


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_empty_loading_error_and_curator_report_states():
    out = _run(f"""
      const ITEMS = {json.dumps(ITEMS)};
      console.log(JSON.stringify({{
        empty: learnedRulesSectionHtml([], {{}}),
        loading: learnedRulesSectionHtml([], {{ loading: true }}),
        error: learnedRulesSectionHtml([], {{ error: 'HTTP 500 <x>' }}),
        noMatch: learnedRulesSectionHtml([ITEMS[3]], {{ filter: 'anti' }}),
        report: learnedRulesSectionHtml(ITEMS, {{ report: {json.dumps(REPORT)} }}),
        junkReport: curatorReportHtml(null) + curatorReportHtml({{}}),
      }}));
    """)
    assert "No learned rules yet" in out["empty"] and "data-lr-add-text" in out["empty"]
    assert "data-lr-error hidden>" in out["empty"]
    assert "Loading learned rules" in out["loading"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-lr-error hidden>" not in out["error"]  # the error is visible
    assert "data-lr-add-text" in out["error"]  # …and the form survives it
    assert "Nothing matches this filter." in out["noMatch"]
    # curator report renders inline with every counter
    report = out["report"]
    assert "data-lr-report" in report
    for key in ("deduped", "inverted", "promoted", "demoted", "pruned"):
        assert key in report
    assert "<b>4</b> pruned" in report and "<b>11</b> active" in report
    assert out["junkReport"].count("data-lr-report") == 1  # null → nothing; {} → zeros
    assert "<b>0</b> deduped" in out["junkReport"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_defensive_payload_unwrapping_and_field_defaults():
    out = _run("""
      console.log(JSON.stringify({
        wrapped: normalizeLearnedItems({ items: [{ id: 'a' }, { id: 'b' }] }).map(i => i.id),
        bare: normalizeLearnedItems([{ id: 'a' }]).map(i => i.id),
        nested: normalizeLearnedItems({ data: { items: [{ id: 'a' }] } }).map(i => i.id),
        junk: normalizeLearnedItems(null),
        drops: normalizeLearnedItems({ items: [null, 'x', {}, { id: 'ok' }] }).map(i => i.id),
        defaults: normalizeLearnedItems([{ id: 'a' }])[0],
        badNums: normalizeLearnedItems([{ id: 'a', effective_score: 'x', harmful_ratio: null }])[0],
        unwrapBare: unwrapLearnedItem({ id: 'a', text: 't' }).id,
        unwrapWrapped: unwrapLearnedItem({ item: { id: 'b', text: 't' } }).id,
        unwrapJunk: unwrapLearnedItem(null),
        unwrapOk: unwrapLearnedItem({ success: true }),
      }));
    """)
    assert out["wrapped"] == ["a", "b"] and out["bare"] == ["a"] and out["nested"] == ["a"]
    assert out["junk"] == [] and out["drops"] == ["ok"]
    assert out["defaults"]["level"] == "semantic" and out["defaults"]["status"] == "active"
    assert out["defaults"]["maturity"] == "candidate" and out["defaults"]["text"] == ""
    assert out["defaults"]["effective_score"] == 0
    assert out["badNums"]["effective_score"] == 0 and out["badNums"]["harmful_ratio"] == 0
    assert out["unwrapBare"] == "a" and out["unwrapWrapped"] == "b"
    assert out["unwrapJunk"] == {}
    # a bare {"success": true} feedback response is not mistaken for an item
    assert out["unwrapOk"] == {"success": True}


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_class_tokens_from_server_values_cannot_break_out_of_the_attribute():
    out = _run("""
      console.log(JSON.stringify({
        row: learnedRuleRowHtml({ id: 'x"><img src=x>', text: 'hi',
                                  level: 'pro" onload="1', maturity: 'PROVEN!!' }),
        token: lrToken('pro" onload="1'),
      }));
    """)
    assert out["token"] == "proonload1"
    assert "<img" not in out["row"] and 'onload="' not in out["row"]
    assert "lr-maturity-proven" in out["row"]
    assert 'data-lr-row="x&quot;&gt;&lt;img src=x&gt;"' in out["row"]
