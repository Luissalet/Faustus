"""The Provenance page (static/js/provenance.js): the 2D AUDIT view over
/api/provenance/*.

The renderers, the force layout and the filters are pure (kept in a marked,
dependency-free region of provenance.js) and run in node; the wiring is pinned
at source level, like the Experts, Objectives and Learned-rules pages.

Four of these tests are the feature's rules, not cosmetics:

  * every step of an evidence chain prints the backend's ``why`` VERBATIM — the
    page never invents a reason of its own;
  * a file step says the LINE it names, and a line the step does not name is
    labelled as the file's own record rather than passed off as this step's;
  * the canvas never draws more than a legible number of nodes, and says
    "showing N of M — narrow the filter" when it holds back; and
  * ``trust`` is printed as it arrived, with anything that is not ``declared``
    marked — an audit view may not quietly mix asserted edges in.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/provenance.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
INDEX = (REPO / "static/index.html").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Provenance: pure helpers"
PURE_END = "// ── Provenance: end pure helpers ──"

NODES = [
    {"id": "memory:a1b2c3", "kind": "memory", "label": "Run the tests <b>first</b>",
     "detail": "procedural · active · proven",
     "meta": {"level": "procedural", "status": "active", "effective_score": 0.82}},
    {"id": "file:src/app.py", "kind": "file", "label": "app.py", "detail": "src/app.py",
     "meta": {"path": "src/app.py", "lines": [8, 120]}},
    {"id": "chat:sess-9", "kind": "chat", "label": "session sess-9", "detail": "",
     "meta": {"session_id": "sess-9000000"}},
    {"id": "objective:OBJ-1", "kind": "objective", "label": "Ship the thing",
     "detail": "open · P1", "meta": {"status": "open", "priority": 1}},
    {"id": "objective:OBJ-3", "kind": "objective", "label": "Blocked on OBJ-1",
     "detail": "blocked · P2", "meta": {"status": "blocked"}},
    {"id": "checkpoint:job-7", "kind": "checkpoint", "label": "dispatch job job-7",
     "detail": "done · 1 file(s) changed", "meta": {"job_id": "job-7", "status": "done"}},
    {"id": "expert:brenner", "kind": "expert", "label": "brenner", "detail": "specialist",
     "meta": {"slug": "brenner"}},
    {"id": "corpus:brenner/craft.pdf", "kind": "corpus", "label": "craft.pdf",
     "detail": "corpus file of the brenner expert",
     "meta": {"slug": "brenner", "source": "craft.pdf"}},
]

EDGES = [
    {"from": "memory:a1b2c3", "to": "file:src/app.py", "kind": "evidence_of",
     "confidence": 0.8, "trust": "declared",
     "why": "this memory's evidence span points at src/app.py:120: \u201crun the tests\u201d",
     "meta": {"line": 120}},
    {"from": "objective:OBJ-3", "to": "objective:OBJ-1", "kind": "depends_on",
     "confidence": 1.0, "trust": "declared",
     "why": "OBJ-3 declares a dependency on OBJ-1 in objectives.jsonl"},
    {"from": "checkpoint:job-7", "to": "file:src/app.py", "kind": "changed", "confidence": 1.0,
     "trust": "declared",
     "why": "the checkpoint diff of job job-7 shows src/app.py was modified "
            "\u2014 observed on disk, not claimed by a worker",
     "meta": {"how": "modified"}},
    {"from": "objective:OBJ-1", "to": "chat:sess-9", "kind": "evidence_of", "confidence": 1.0,
     "trust": "declared", "why": "OBJ-1 was ADDed from this chat session on 2026-09-01"},
    {"from": "memory:a1b2c3", "to": "corpus:brenner/craft.pdf", "kind": "cites",
     "confidence": 1.0, "trust": "declared",
     "why": "stored evidence cites craft.pdf at page 42 (chunk c0123456789)"},
    {"from": "expert:brenner", "to": "corpus:brenner/craft.pdf", "kind": "contains",
     "confidence": 1.0, "trust": "declared",
     "why": "craft.pdf is a file in the brenner expert's own corpus"},
    # An edge whose endpoint was never created: the builder drops these, and so
    # must the page — a line into nothing is worse than a missing line.
    {"from": "memory:a1b2c3", "to": "memory:ghost", "kind": "duplicate_of", "confidence": 0.7,
     "trust": "declared", "why": "70% shared"},
]

GRAPH = {
    "status": "success", "nodes": NODES, "edges": EDGES,
    "sources": {"objectives": {"available": True, "count": 2, "note": "2 objective(s), 1 edge(s)"},
                "experts": {"available": False, "count": 0,
                            "note": "no expert index was readable"}},
    "truncated": True,
    "stats": {"nodes": 8, "edges": 6, "orphans": 0},
    "node_kinds": ["objective", "memory", "chat", "file", "checkpoint", "expert", "corpus"],
    "edge_kinds": ["depends_on", "evidence_of", "contradicts", "changed", "cites", "contains",
                   "duplicate_of"],
    "enabled": True, "limit": 2000,
}

EXPLAIN = {
    "status": "success",
    "node": NODES[0],
    "summary": "Run the tests first \u2014 3 declared record(s) explain this node.",
    "steps": [
        {"order": 1, "hop": 1, "from": "memory:a1b2c3", "to": "file:src/app.py",
         "kind": "evidence_of", "confidence": 0.8, "trust": "declared",
         "why": "this memory's evidence span points at src/app.py:120: \u201crun the tests\u201d",
         "direction": "rests_on", "node": NODES[1]},
        {"order": 2, "hop": 1, "from": "memory:a1b2c3", "to": "corpus:brenner/craft.pdf",
         "kind": "cites", "confidence": 1.0, "trust": "declared",
         "why": "stored evidence cites craft.pdf at page 42 (chunk c0123456789)",
         "direction": "rests_on", "node": NODES[7]},
        {"order": 3, "hop": 2, "from": "checkpoint:job-7", "to": "file:src/app.py",
         "kind": "changed", "confidence": 1.0, "trust": "inferred",
         "why": "the checkpoint diff of job job-7 shows src/app.py was modified",
         "direction": "vouches_for", "node": NODES[5]},
    ],
    "enabled": True,
}

ORPHANS = {
    "status": "success",
    "orphans": {"memory": [{"id": "memory:zz9", "kind": "memory", "label": "A floating <i>rule</i>",
                            "detail": "semantic · active"}],
                "file": [{"id": "file:notes/old.md", "kind": "file", "label": "old.md",
                          "detail": "notes/old.md", "meta": {"path": "notes/old.md"}}]},
    "orphan_ids": ["file:notes/old.md", "memory:zz9"],
    "count": 2,
    "duplicates": [{
        "a": "memory:a1b2c3", "b": "memory:d4e5f6",
        "a_label": "Run the tests before committing",
        "b_label": "run the tests before committing!",
        "ratio": 0.93,
        "why": "93% of the two texts is literally shared \u2014 verified by exact substring "
               "comparison, not by a model: \u201crun the tests before committing\u201d",
        "spans": [[[0, 31], [0, 31]], [[4, 9], [6, 11]]],
    }],
    "stats": {"nodes": 12, "edges": 6, "orphans": 2},
    "enabled": True,
}

NEIGHBORS = {
    "status": "success", "root": "objective:OBJ-1", "hops": 2,
    "nodes": NODES, "edges": EDGES[:6],
    "impact": [NODES[4]], "impact_ids": ["objective:OBJ-3", "objective:OBJ-9"],
    "enabled": True,
}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from provenance.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/provenance.js")],
                          capture_output=True).returncode == 0
    # no inline handlers, no native dialogs, no external graph library
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    for forbidden in ("cdn.", "d3.", "import * as d3", "vis-network", "cytoscape"):
        assert forbidden not in SRC, forbidden
    assert SRC.count("import ") == 0, "the page must stay self-contained"
    # every endpoint this page reads, all under the one request helper
    assert "/api/provenance" in SRC
    for path in ("/graph${graphQuery(state)}", "/explain${scopeQuery(state)}",
                 "/neighbors${scope}${join}hops=", "/orphans${scopeQuery(state)}"):
        assert path in SRC, path
    # exported entry points + the pure helpers the tests below drive
    for entry in ("export async function openProvenancePanel",
                  "export function closeProvenancePanel",
                  "export async function loadGraph", "export async function openNode",
                  "export async function openNeighbors"):
        assert entry in SRC, entry
    for fn in ("explainPanelHtml", "explainStepHtml", "explainTerminus", "stepWhereText",
               "stepLineInfo", "linesFromWhy", "graphPanelHtml", "graphSvgHtml",
               "graphViewModel", "graphLegendHtml", "graphNoticeHtml", "edgeKeyHtml",
               "layoutGraph", "seedPositions", "capGraph", "filterGraphByKinds",
               "pickLabelIds", "matchesQuery", "neighborsPanelHtml", "impactListHtml",
               "orphansPanelHtml", "duplicateRowHtml", "duplicateExcerpt",
               "duplicateSpansText", "normalizeGraph", "normalizeExplain",
               "normalizeNeighbors", "normalizeOrphans", "encodeNodeId", "graphQuery",
               "toolbarHtml", "sourcesHtml", "nodeTooltipText"):
        assert f"function {fn}" in SRC, fn
    # delegated listeners on the modal, not per-row handlers
    assert "modal.addEventListener('click'" in SRC and "modal.addEventListener('input'" in SRC
    assert "modal.addEventListener('keydown'" in SRC
    assert "modal.addEventListener('wheel'" in SRC and "modal.addEventListener('pointerdown'" in SRC
    # errors land inline, not in a dialog — and are actually used
    assert "data-pv-error" in SRC and "function inlineError" in SRC
    assert SRC.count("inlineError(") > 2
    # a clearly delimited CSS block using theme tokens exists
    assert "/* ── Provenance ──" in CSS
    for selector in (".provenance-page", ".provenance-split", ".pv-canvas", ".pv-svg",
                     ".pv-node.is-match .pv-dot", ".pv-edge-changed", ".pv-step-why",
                     ".pv-step-trust.is-inferred", ".pv-terminus", ".pv-dup-ratio",
                     ".pv-legend-chip.is-on", ".pv-impact-row", ".pv-btn:hover:not(:disabled)"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Provenance ──", 1)[1]
    assert "#" not in block.split("*/", 1)[1], "hardcoded hex colour in the Provenance block"


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


def test_the_page_has_an_entry_point_and_a_modal_shell():
    assert 'id="tool-provenance-btn"' in INDEX and ">Provenance</span>" in INDEX
    assert 'id="provenance-modal"' in INDEX
    for slot in ("provenance-toolbar", "provenance-main", "provenance-side"):
        assert f'id="{slot}"' in INDEX, slot
    assert 'aria-label="Close provenance"' in INDEX and 'id="close-provenance-modal"' in INDEX
    assert "/static/js/provenance.js" in INDEX


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_node_ids_with_colons_and_slashes_survive_the_path_route():
    out = _run("""
      console.log(JSON.stringify({
        file: encodeNodeId('file:src/app.py'),
        nested: encodeNodeId('corpus:brenner/notes & more.md'),
        memory: encodeNodeId('memory:a1b2c3'),
        space: encodeNodeId('file:my docs/a b.txt'),
        junk: encodeNodeId(null),
      }));
    """)
    # the ":" is encoded, the "/" stays a path separator (the route is :path)
    assert out["file"] == "file%3Asrc/app.py"
    assert out["nested"] == "corpus%3Abrenner/notes%20%26%20more.md"
    assert out["memory"] == "memory%3Aa1b2c3"
    assert out["space"] == "file%3Amy%20docs/a%20b.txt"
    assert out["junk"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_query_carries_the_scope_the_kind_filter_and_the_budget():
    out = _run("""
      console.log(JSON.stringify({
        empty: graphQuery({}),
        full: graphQuery({ project: 'p 1', workspace: 'C:/code/x', kinds: ['file', 'MEMORY!'], limit: 500 }),
        zero: graphQuery({ limit: 0 }),
        scope: scopeQuery({ project: 'p1', workspace: 'w' }),
        scopeEmpty: scopeQuery(null),
        junk: graphQuery(null),
      }));
    """)
    assert out["empty"] == "" and out["junk"] == "" and out["zero"] == ""
    assert out["full"] == "?project=p%201&workspace=C%3A%2Fcode%2Fx&kinds=file%2Cmemory&limit=500"
    assert out["scope"] == "?project=p1&workspace=w" and out["scopeEmpty"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_evidence_chain_prints_every_why_verbatim_and_ends_at_the_file_and_line():
    out = _run(f"""
      const EXPLAIN = {json.dumps(EXPLAIN)};
      console.log(JSON.stringify({{
        html: explainPanelHtml(EXPLAIN, {{}}),
        terminus: explainTerminus(EXPLAIN.steps),
      }}));
    """)
    html = out["html"]
    # the summary and every `why` arrive verbatim (escaped, never reworded)
    assert "3 declared record(s) explain this node." in html
    assert "this memory&#39;s evidence span points at src/app.py:120: “run the tests”" in html
    assert "stored evidence cites craft.pdf at page 42 (chunk c0123456789)" in html
    assert "the checkpoint diff of job job-7 shows src/app.py was modified" in html
    # ordered steps, each carrying its hop, kind, direction, confidence and trust
    assert html.index('data-pv-step="1"') < html.index('data-pv-step="2"') < html.index('data-pv-step="3"')
    assert "hop 1" in html and "hop 2" in html
    assert "evidence_of" in html and "cites" in html and "changed" in html
    assert "rests on" in html and "vouches for it" in html
    assert "conf 0.80" in html and "conf 1.00" in html
    assert '<span class="pv-step-trust is-declared"' in html
    # trust that is NOT declared is marked rather than blended in
    assert '<span class="pv-step-trust is-inferred"' in html and ">inferred<" in html
    # the chain ends at the file AND the line the step named
    assert "src/app.py line 120" in html
    assert "Traced to" in html and 'data-pv-node-open="file:src/app.py"' in html
    assert out["terminus"]["text"] == "src/app.py line 120"
    assert out["terminus"]["lines"] == [120]
    # the node's own stored fields are shown as stored
    assert "procedural" in html and "0.82" in html
    # labels are escaped
    assert "Run the tests &lt;b&gt;first&lt;/b&gt;" in html and "<b>first</b>" not in html
    # and the two follow-on questions are one click away
    assert 'data-pv-neighbors="memory:a1b2c3"' in html and "What breaks if I touch this" in html
    assert "onclick=" not in html


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_file_step_says_the_line_it_names_and_never_borrows_another_one():
    out = _run(f"""
      const FILE = {json.dumps(NODES[1])};
      const fromEdge = {{ order: 1, hop: 1, kind: 'evidence_of', direction: 'rests_on',
                          why: 'span points at src/app.py:120', node: FILE, meta: {{ line: 42 }} }};
      const fromWhy = {{ order: 1, hop: 1, kind: 'evidence_of', direction: 'rests_on',
                         why: 'this memory’s evidence span points at src/app.py:120: “x”', node: FILE }};
      const fromNode = {{ order: 1, hop: 1, kind: 'evidence_of', direction: 'rests_on',
                          why: 'this memory was recorded from chat session abc', node: FILE }};
      const noLine = {{ order: 1, hop: 1, kind: 'changed', direction: 'vouches_for',
                        why: 'job 7 changed it',
                        node: {{ id: 'file:a.py', kind: 'file', label: 'a.py', detail: 'a.py', meta: {{ path: 'a.py' }} }} }};
      const chat = {{ order: 1, hop: 1, kind: 'evidence_of', direction: 'rests_on', why: 'x',
                      node: {json.dumps(NODES[2])} }};
      const job = {{ order: 1, hop: 1, kind: 'changed', direction: 'vouches_for', why: 'x',
                     node: {json.dumps(NODES[5])} }};
      const titledJob = {{ order: 1, hop: 1, kind: 'changed', direction: 'vouches_for', why: 'x',
                           node: {{ id: 'checkpoint:job-7', kind: 'checkpoint',
                                    label: 'Build the graph', meta: {{ job_id: 'job-7' }} }} }};
      const corpus = {{ order: 1, hop: 1, kind: 'cites', direction: 'rests_on', why: 'x',
                        node: {json.dumps(NODES[7])} }};
      console.log(JSON.stringify({{
        titledJob: stepWhereText(titledJob), corpus: stepWhereText(corpus),
        edge: stepLineInfo(fromEdge), edgeText: stepWhereText(fromEdge),
        why: stepLineInfo(fromWhy), whyText: stepWhereText(fromWhy),
        node: stepLineInfo(fromNode), nodeText: stepWhereText(fromNode),
        none: stepWhereText(noLine),
        chat: stepWhereText(chat), job: stepWhereText(job),
        notALine: linesFromWhy({{ why: 'chunk c0123456789:42 and src/other.py:9', node: FILE }}),
      }}));
    """)
    # the edge's own meta wins, then the sentence, then the file node's record
    assert out["edge"] == {"lines": [42], "source": "edge"}
    assert out["edgeText"] == "src/app.py line 42"
    assert out["why"] == {"lines": [120], "source": "why"}
    assert out["whyText"] == "src/app.py line 120"
    # a line this step does NOT name is labelled as the file's own record
    assert out["node"] == {"lines": [8, 120], "source": "node"}
    assert out["nodeText"] == "src/app.py — lines recorded here: 8, 120"
    assert out["none"] == "a.py"
    # a "where" that would only repeat the label is left out
    assert out["chat"] == "chat session sess-9000000"
    assert out["job"] == ""
    assert out["titledJob"] == "dispatch job job-7"
    assert out["corpus"] == "brenner corpus"
    # a number attached to another path is never adopted as this file's line
    assert out["notALine"] == []


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_chain_with_nothing_in_it_says_so_instead_of_looking_empty():
    out = _run("""
      console.log(JSON.stringify({
        none: explainPanelHtml({ node: { id: 'memory:x', kind: 'memory', label: 'Lonely' },
                                 steps: [],
                                 summary: 'Lonely — nothing stored points at this node.' }, {}),
        nothingPicked: explainPanelHtml(null, {}),
        loading: explainPanelHtml(null, { loading: true }),
        error: explainPanelHtml(null, { error: 'HTTP 404 <x>' }),
      }));
    """)
    assert "nothing stored points at this node" in out["none"]
    assert "in this graph it is an orphan" in out["none"]
    assert "No chat, file or job record ends this chain" in out["none"]
    assert "Why does the agent believe this?" in out["nothingPicked"]
    assert "Following the evidence" in out["loading"]
    assert "HTTP 404 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_layout_is_deterministic_and_stays_inside_the_box():
    out = _run(f"""
      const G = {json.dumps(GRAPH)};
      const g = normalizeGraph(G);
      const a = layoutGraph(g.nodes, g.edges, {{}});
      const b = layoutGraph(g.nodes.slice(), g.edges.slice(), {{}});
      const one = layoutGraph([g.nodes[0]], [], {{}});
      const bounds = Object.values(a.positions);
      console.log(JSON.stringify({{
        same: JSON.stringify(a) === JSON.stringify(b),
        count: Object.keys(a.positions).length,
        inside: bounds.every(p => p.x >= 0 && p.x <= a.width && p.y >= 0 && p.y <= a.height),
        finite: bounds.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)),
        distinct: new Set(bounds.map(p => `${{p.x}},${{p.y}}`)).size,
        one: one.positions[g.nodes[0].id],
        empty: layoutGraph([], [], {{}}),
        junk: layoutGraph(null, null, {{}}),
      }}));
    """)
    assert out["same"], "the same graph must lay out the same way twice"
    assert out["count"] == 8 and out["inside"] and out["finite"]
    assert out["distinct"] == 8, "nodes must not pile up on one point"
    assert out["one"] == {"x": 500, "y": 340}
    assert out["empty"]["positions"] == {} and out["junk"]["positions"] == {}


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_canvas_caps_what_it_draws_and_says_how_much_it_is_holding_back():
    out = _run("""
      const nodes = [];
      const edges = [];
      for (let i = 0; i < 640; i += 1) {
        nodes.push({ id: `memory:${String(i).padStart(3, '0')}`, kind: i % 4 ? 'memory' : 'file',
                     label: `node ${i}` });
      }
      // node 000 is the hub: it must survive the cap on degree alone.
      for (let i = 1; i < 300; i += 1) {
        edges.push({ from: 'memory:000', to: `memory:${String(i).padStart(3, '0')}`,
                     kind: 'evidence_of', confidence: 1, trust: 'declared', why: 'x' });
      }
      const big = { nodes, edges };
      const view = graphViewModel(big, {});
      const small = graphViewModel(big, { drawLimit: 5 });
      console.log(JSON.stringify({
        shown: view.shown, total: view.total, capped: view.capped,
        hubKept: view.nodes.some(n => n.id === 'memory:000'),
        labels: view.labels.length,
        notice: graphNoticeHtml(view, big),
        smallShown: small.shown,
        smallHub: small.nodes.map(n => n.id),
        tinyNotice: graphNoticeHtml(graphViewModel({ nodes: nodes.slice(0, 3), edges: [] }, {}),
                                    { nodes: nodes.slice(0, 3), edges: [] }),
      }));
    """)
    assert out["shown"] == 200 and out["total"] == 640 and out["capped"] is True
    assert out["hubKept"], "the best-connected node must survive the cap"
    assert out["labels"] <= 24, "a crowded canvas only labels the nodes that matter"
    assert "Showing 200 of 640 nodes" in out["notice"] and "narrow the filter" in out["notice"]
    assert out["smallShown"] == 5 and "memory:000" in out["smallHub"]
    assert "Showing all 3 node(s)" in out["tinyNotice"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_server_truncation_flag_is_repeated_to_the_user():
    out = _run(f"""
      const G = {json.dumps(GRAPH)};
      const view = graphViewModel(G, {{}});
      console.log(JSON.stringify({{
        truncated: graphNoticeHtml(view, G),
        whole: graphNoticeHtml(view, {{ ...G, truncated: false }}),
      }}));
    """)
    assert "stopped building at its node budget (2000)" in out["truncated"]
    assert "partial graph" in out["truncated"]
    assert "node budget" not in out["whole"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_canvas_colours_by_kind_styles_edges_by_kind_and_highlights_a_search():
    out = _run(f"""
      const G = {json.dumps(GRAPH)};
      const plain = graphViewModel(G, {{}});
      const searched = graphViewModel(G, {{ query: 'app.py' }});
      const filtered = graphViewModel(G, {{ kinds: ['objective'] }});
      console.log(JSON.stringify({{
        svg: graphSvgHtml(plain, layoutGraph(plain.nodes, plain.edges, {{}}), {{}}),
        mini: graphSvgHtml(plain, layoutGraph(plain.nodes, plain.edges, {{}}), {{ prefix: 'pvmini' }}),
        hits: searched.matched,
        searchSvg: graphSvgHtml(searched, layoutGraph(searched.nodes, searched.edges, {{}}), {{}}),
        selectedSvg: graphSvgHtml(graphViewModel(G, {{ selected: 'file:src/app.py' }}),
                                  layoutGraph(plain.nodes, plain.edges, {{}}), {{}}),
        kinds: filtered.nodes.map(n => n.id),
        edgesAfterFilter: filtered.edges.length,
        legend: graphLegendHtml(G, {{ kinds: ['file'] }}),
        panel: graphPanelHtml(G, {{}}),
        off: graphPanelHtml({{ ...G, enabled: false }}, {{}}),
        emptyGraph: graphPanelHtml({{ nodes: [], edges: [], sources: G.sources }}, {{}}),
        failed: graphPanelHtml({{ nodes: [], edges: [] }}, {{ error: 'HTTP 403 <x>' }}),
        failedOrphans: orphansPanelHtml(null, {{ error: 'HTTP 403 <x>' }}),
      }}));
    """)
    svg = out["svg"]
    # one class per node kind, one line style per edge kind
    for kind in ("memory", "file", "chat", "objective", "checkpoint", "expert", "corpus"):
        assert f"pv-kind-{kind}" in svg, kind
    for kind in ("evidence_of", "depends_on", "changed", "cites", "contains"):
        assert f"pv-edge-{kind}" in svg, kind
    # the dangling edge the builder would have dropped is not drawn
    assert "memory:ghost" not in svg
    assert svg.count("<line") == 6
    # every edge carries its own `why` as a native tooltip
    assert "<title>memory:a1b2c3 evidence_of file:src/app.py — this memory&#39;s evidence" in svg
    # clicking is a data attribute, and the node is keyboard reachable
    assert 'data-pv-node="file:src/app.py"' in svg and 'tabindex="0"' in svg
    assert 'role="button"' in svg and "onclick=" not in svg
    # the arrow marker is namespaced so a second canvas can coexist
    assert 'id="pv-arrow"' in svg and 'marker-end="url(#pv-arrow)"' in svg
    assert 'id="pvmini-arrow"' in out["mini"] and 'marker-end="url(#pvmini-arrow)"' in out["mini"]
    # search highlights the hits and dims the rest; selection gets its own ring
    assert out["hits"] == ["file:src/app.py"]
    assert "is-match" in out["searchSvg"] and "is-dim" in out["searchSvg"]
    assert "is-selected" in out["selectedSvg"]
    # a kind filter keeps only that kind, and takes its dangling edges with it
    assert out["kinds"] == ["objective:OBJ-1", "objective:OBJ-3"]
    assert out["edgesAfterFilter"] == 1
    # the legend is the filter: counts, pressed state, and an edge key
    assert 'data-pv-kind="memory"' in out["legend"] and "<b>1</b>" in out["legend"]
    assert 'data-pv-kind="file"' in out["legend"] and 'aria-pressed="true"' in out["legend"]
    assert "data-pv-kind-clear" in out["legend"]
    assert 'class="pv-edge-key"' in out["legend"] and "pv-edge-key-swatch" in out["legend"]
    # the panel wires the canvas, the tooltip and the source report
    assert "data-pv-canvas" in out["panel"] and "data-pv-tip" in out["panel"]
    assert "pv-sources" in out["panel"] and "1 of 2 source(s) readable" in out["panel"]
    assert "no expert index was readable" in out["panel"]
    # the feature toggle reads as "turned off", never as an empty workspace
    assert "turned off in Settings" in out["off"] and "<svg" not in out["off"]
    assert "Nothing to draw yet" in out["emptyGraph"]
    # a read that failed reads as a failure, never as an empty workspace
    assert "HTTP 403 &lt;x&gt;" in out["failed"] and "Nothing to draw yet" not in out["failed"]
    assert "HTTP 403 &lt;x&gt;" in out["failedOrphans"]
    assert "No orphans" not in out["failedOrphans"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_search_matches_every_term_across_label_detail_and_path():
    out = _run(f"""
      const NODE = {json.dumps(NODES[1])};
      console.log(JSON.stringify({{
        label: matchesQuery(NODE, 'app'),
        path: matchesQuery(NODE, 'src/app'),
        both: matchesQuery(NODE, 'app src'),
        miss: matchesQuery(NODE, 'app nope'),
        empty: matchesQuery(NODE, '   '),
        junk: matchesQuery(null, 'x'),
        upper: matchesQuery(NODE, 'APP.PY'),
      }}));
    """)
    assert out["label"] and out["path"] and out["both"] and out["upper"]
    assert not out["miss"] and not out["empty"] and not out["junk"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_orphans_are_grouped_by_kind_and_duplicates_show_the_verified_span():
    out = _run(f"""
      const ORPHANS = {json.dumps(ORPHANS)};
      console.log(JSON.stringify({{
        html: orphansPanelHtml(ORPHANS, {{}}),
        excerpt: duplicateExcerpt(ORPHANS.duplicates[0].why),
        noExcerpt: duplicateExcerpt('70% is shared'),
        spans: duplicateSpansText(ORPHANS.duplicates[0].spans),
        noSpans: duplicateSpansText(null),
        empty: orphansPanelHtml({{ orphans: {{}}, duplicates: [], count: 0 }}, {{}}),
        loading: orphansPanelHtml(null, {{ loading: true }}),
      }}));
    """)
    html = out["html"]
    # orphans, grouped by kind, each openable
    assert "<h3>Orphans <b>2</b></h3>" in html
    assert "<summary>memory <b>1</b></summary>" in html and "<summary>file <b>1</b></summary>" in html
    assert 'data-pv-node-open="memory:zz9"' in html and 'data-pv-node-open="file:notes/old.md"' in html
    assert "A floating &lt;i&gt;rule&lt;/i&gt;" in html and "<i>rule</i>" not in html
    # duplicates: both labels, the measured ratio, the why verbatim, the span
    assert "Run the tests before committing" in html and "run the tests before committing!" in html
    assert ">93%<" in html
    assert "verified by exact substring comparison, not by a model" in html
    assert "verified shared text" in html
    assert "<q>run the tests before committing</q>" in html
    assert "2 verified span(s), in normalized characters: 0–31 ↔ 0–31, 4–9 ↔ 6–11" in html
    assert out["excerpt"] == "run the tests before committing"
    assert out["noExcerpt"] == ""
    assert out["spans"].startswith("2 verified span(s)") and out["noSpans"] == ""
    # honest empty states
    assert "No orphans: every node is connected" in out["empty"]
    assert "Nothing is said twice" in out["empty"]
    assert "Looking for what is floating" in out["loading"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_neighbours_panel_answers_what_breaks_if_i_touch_this():
    out = _run(f"""
      const NB = {json.dumps(NEIGHBORS)};
      console.log(JSON.stringify({{
        html: neighborsPanelHtml(NB, {{}}),
        none: neighborsPanelHtml({{ ...NB, impact: [], impact_ids: [] }}, {{}}),
        empty: impactListHtml(null),
      }}));
    """)
    html = out["html"]
    assert "What breaks if I touch this" in html
    # the impact list keeps an id the subgraph did not carry a node for
    assert 'data-pv-node-open="objective:OBJ-3"' in html
    assert 'data-pv-node-open="objective:OBJ-9"' in html
    assert "2 node(s) reachable by reversed" in html
    # 1–3 hops, with the current one pressed
    assert 'data-pv-hops="1"' in html and 'data-pv-hops="3"' in html
    assert 'data-pv-hops="2" aria-pressed="true"' in html
    # the subgraph is drawn with its own marker namespace, root selected
    assert 'id="pvmini-arrow"' in html and "pv-canvas-mini" in html
    assert "is-selected" in html
    assert "Nothing declared depends on this node" in out["none"]
    assert "Nothing declared depends on this node" in out["empty"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_defensive_parsing_of_bare_wrapped_and_broken_payloads():
    out = _run("""
      const NODE = { id: 'memory:a', kind: 'memory', label: 'x' };
      const EDGE = { from: 'memory:a', to: 'file:b.py', kind: 'evidence_of', why: 'w' };
      console.log(JSON.stringify({
        wrapped: normalizeGraph({ status: 'success', nodes: [NODE], edges: [] }).nodes.length,
        nested: normalizeGraph({ data: { nodes: [NODE], edges: [] } }).nodes.length,
        bare: normalizeGraph([NODE]).nodes.length,
        junk: normalizeGraph(null),
        drops: normalizeGraph({ nodes: [null, 'x', {}, NODE] }).nodes.map(n => n.id),
        dangling: normalizeGraph({ nodes: [NODE], edges: [EDGE] }).edges.length,
        defaults: normalizeGraph({ nodes: [{ id: 'file:b.py' }] }).nodes[0],
        badConfidence: normalizeGraph({ nodes: [NODE, { id: 'file:b.py' }],
                                        edges: [{ ...EDGE, confidence: 'nope' }] }).edges[0],
        statsFallback: normalizeGraph({ nodes: [NODE, { id: 'file:b.py' }], edges: [] }).stats,
        explainBare: normalizeExplain({ node: NODE, steps: [{}] }).steps[0],
        explainJunk: normalizeExplain(null),
        orphansJunk: normalizeOrphans(null),
        orphanCount: normalizeOrphans({ orphans: { memory: [NODE, null] } }).count,
        neighborsJunk: normalizeNeighbors(null),
        spansJunk: normalizeOrphans({ duplicates: [{ a: 'x', b: 'y', spans: [1, [[0]], [[0,1],[2,3]]] }] }).duplicates[0].spans,
      }));
    """)
    assert out["wrapped"] == 1 and out["nested"] == 1 and out["bare"] == 1
    assert out["junk"]["nodes"] == [] and out["junk"]["edges"] == []
    assert out["drops"] == ["memory:a"]
    assert out["dangling"] == 0, "an edge into a node that is not there must be dropped"
    # a node with nothing but an id still renders: the label falls back to the id
    assert out["defaults"]["label"] == "file:b.py" and out["defaults"]["kind"] == "node"
    assert out["defaults"]["detail"] == "" and out["defaults"]["meta"] == {}
    assert out["badConfidence"]["confidence"] is None
    assert out["badConfidence"]["trust"] == "declared"
    assert out["statsFallback"]["nodes"] == 2 and out["statsFallback"]["orphans"] == 2
    assert out["explainBare"]["order"] == 1 and out["explainBare"]["hop"] == 1
    assert out["explainBare"]["direction"] == "rests_on" and out["explainBare"]["node"] is None
    assert out["explainJunk"]["node"] is None and out["explainJunk"]["steps"] == []
    assert out["orphansJunk"]["orphans"] == {} and out["orphansJunk"]["duplicates"] == []
    assert out["orphanCount"] == 1
    assert out["neighborsJunk"]["nodes"] == [] and out["neighborsJunk"]["impact"] == []
    assert out["spansJunk"] == [[[0, 1], [2, 3]]]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_server_values_cannot_break_out_of_an_attribute_or_a_class():
    out = _run("""
      const nasty = { id: 'file:x"><img src=x>', kind: 'me"mory onload="1',
                      label: '<script>alert(1)</script>', detail: '"><b>', meta: { path: '"x' } };
      const view = graphViewModel({ nodes: [nasty], edges: [] }, {});
      console.log(JSON.stringify({
        svg: graphSvgHtml(view, layoutGraph(view.nodes, [], {}), {}),
        prefix: graphSvgHtml(view, layoutGraph(view.nodes, [], {}), { prefix: 'a"><b' }),
        tip: nodeTooltipText(nasty),
        chip: explainStepHtml({ kind: 'ev"il', why: '<b>why</b>', node: nasty, direction: 'rests_on' }),
      }));
    """)
    for html in (out["svg"], out["prefix"], out["chip"]):
        assert "<img" not in html and "<script>" not in html and 'onload="' not in html
    assert "pv-kind-memoryonload1" in out["svg"], "a class token is sanitized, not escaped"
    assert 'id="ab-arrow"' in out["prefix"]
    assert 'data-pv-node="file:x&quot;&gt;&lt;img src=x&gt;"' in out["svg"]
    assert "pv-step-evil" in out["chip"] and "&lt;b&gt;why&lt;/b&gt;" in out["chip"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_toolbar_keeps_the_scope_the_search_and_the_two_tabs():
    out = _run("""
      console.log(JSON.stringify({
        graph: toolbarHtml({ tab: 'graph', query: 'a<b', project: 'p1', workspace: 'C:/x' }),
        orphans: toolbarHtml({ tab: 'orphans' }),
        error: toolbarHtml({ error: 'HTTP 403 <x>' }),
      }));
    """)
    graph = out["graph"]
    assert 'data-pv-tab="graph"' in graph and 'data-pv-tab="orphans"' in graph
    assert 'class="pv-tab is-on" data-pv-tab="graph"' in graph
    assert 'value="a&lt;b"' in graph and 'value="p1"' in graph and 'value="C:/x"' in graph
    assert "data-pv-search" in graph and "data-pv-reload" in graph and "data-pv-zoom-reset" in graph
    assert "data-pv-error hidden>" in graph
    assert 'class="pv-tab is-on" data-pv-tab="orphans"' in out["orphans"]
    assert "HTTP 403 &lt;x&gt;" in out["error"] and "data-pv-error hidden>" not in out["error"]
