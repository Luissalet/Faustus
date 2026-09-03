"""The Experts page (static/js/experts.js): the gallery + editor over
/api/experts/*, and the review panel that renders one ``expert_review`` result
as track changes.

The renderers are pure (kept in a marked, dependency-free region of
experts.js) and run in node; the wiring is pinned at source level, like the
Objectives and Learned-rules pages.

Two of these tests are the feature's honesty rules, not cosmetics:

  * a correction's ``label`` is printed VERBATIM — the panel may never decide
    for itself what ``anchored`` means; and
  * a citation without a page renders "page unknown". A page number that is
    not in the result is not a page number.

And one is a contract with the backend: ``applyAcceptedDeltas`` must produce
exactly what ``src.expert_review.apply_deltas`` produces, for the same deltas,
or "Copy result" hands the user a different text than the agent's ``apply``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/experts.js").read_text(encoding="utf-8")
MENTIONS = (REPO / "static/js/fileMentions.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
INDEX = (REPO / "static/index.html").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Experts: pure helpers"
PURE_END = "// ── Experts: end pure helpers ──"

ROWS = [
    {"slug": "brenner_bot", "name": "Brenner <b>on craft</b>", "description": "Scene rules",
     "model": "llama3", "enabled": True, "corpus_files": 2, "chunks": 41,
     "invocations": 7, "accepted": 5, "rejected": 1},
    {"slug": "lexicon", "name": "Lexicon", "description": "Word choice", "model": "",
     "enabled": True, "corpus_files": 1, "chunks": 9, "accepted": 9, "rejected": 0},
    {"slug": "archivist", "name": "Archivist", "description": "", "model": "",
     "enabled": False, "corpus_files": 0, "chunks": 0, "accepted": 0, "rejected": 0},
]

DETAIL = {
    "expert": {"slug": "brenner_bot", "name": "Brenner", "description": "Craft",
               "model": "llama3", "temperature": 0.2, "top_p": 1.0, "enabled": True,
               "instructions": "Read <closely>", "rubric": ["no adverbs", "cut throat-clearing"]},
    "usage": {"invocations": 3, "accepted": 5, "rejected": 1, "last_used": None},
    "files": [{"name": "brenner.pdf", "bytes": 2400000, "pages": 320, "chunks": 40},
              {"name": "notes & more.md", "bytes": 900, "pages": None, "chunks": 1}],
    "chunks": 41, "indexed_at": "2026-09-02T10:00:00",
    "collection": "odysseus_expert_brenner_bot",
}

REVIEW = {
    "expert": {"slug": "brenner_bot", "name": "Brenner", "model": "llama3"},
    "deltas": [
        {"id": "D1", "op": "EDIT", "span": {"start": 4, "end": 9}, "quote": "quick",
         "replacement": "swift", "rationale": "sharper <verb>", "rule": "no filler",
         "severity": "high",
         "citations": [{"marker": "C1", "chunk_id": "c-1", "source": "brenner.pdf",
                        "page": 42, "page_label": "page 42",
                        "ref": "brenner.pdf, page 42", "known": True, "supports": True}],
         "anchored": True, "label": "corpus", "confidence": 0.9,
         "relocated": False, "notes": []},
        {"id": "D2", "op": "KILL", "span": {"start": 15, "end": 20}, "quote": " lazy",
         "replacement": "", "rationale": "adverbial", "rule": "", "severity": "low",
         "citations": [{"marker": "C2", "chunk_id": "c-2", "source": "notes.md",
                        "page": None, "page_label": "page unknown",
                        "ref": "notes.md, page unknown", "known": True, "supports": False}],
         "anchored": False, "label": "model's opinion, not the corpus",
         "confidence": 0.25, "relocated": True, "notes": ["quote relocated"]},
    ],
    "rejected": [{"id": "D3", "op": "EDIT", "reason": "quote not found in the original text",
                  "raw": {"quote": "a ghost <line>"}}],
    "anchored_count": 1, "opinion_count": 1, "degraded": True, "chunks": 1,
    "errors": [], "text": "The quick brown lazy dog",
}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from experts.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/experts.js")],
                          capture_output=True).returncode == 0
    # no inline handlers, no native dialogs (deletion goes through styledConfirm)
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "styledConfirm" in SRC and "data-exp-delete" in SRC and "data-exp-file-delete" in SRC
    # every endpoint this page needs, all under the one request helper
    assert "/api/experts" in SRC
    for path in ("/corpus`", "/reindex`", "/search?q=", "/block?q=", "/feedback?accepted="):
        assert path in SRC, path
    assert "method: 'PATCH'" in SRC and "method: 'DELETE'" in SRC
    # the upload is multipart with the field name the route expects
    assert "new FormData()" in SRC and "form.append('files'" in SRC
    # exported entry points + the pure helpers the tests below drive
    for entry in ("export async function openExpertsPanel", "export function closeExpertsPanel",
                  "export function openReviewPanel", "export async function loadExperts"):
        assert entry in SRC, entry
    for fn in ("expertsGalleryHtml", "expertCardHtml", "expertDetailHtml", "corpusFileRowHtml",
               "searchHitsHtml", "blockPreviewHtml", "reindexSummaryHtml", "reviewPanelHtml",
               "deltaCardHtml", "markedTextHtml", "rejectedListHtml", "applyAcceptedDeltas",
               "normalizeExperts", "normalizeDetail", "normalizeReview", "sortExperts",
               "filterExperts", "pageLabelOf", "expertMentionRows"):
        assert f"function {fn}" in SRC, fn
    # delegated listeners on the modal, not per-row handlers
    assert "modal.addEventListener('click'" in SRC and "modal.addEventListener('submit'" in SRC
    # errors land inline, not in a dialog — and are actually used, not just declared
    assert "data-exp-error" in SRC and "function inlineError" in SRC
    assert SRC.count("inlineError(") > 2
    # a save that fails, or a corpus action that re-renders, must not throw the
    # user's unsaved edits away
    assert "data-exp-save" in SRC and "function captureForm" in SRC
    assert "refreshDetail(true)" in SRC
    # the honesty rule is in the source, not only in the rendered output
    assert "delta.label" in SRC


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


def test_the_page_has_an_entry_point_and_a_modal_shell():
    assert 'id="tool-experts-btn"' in INDEX and ">Experts</span>" in INDEX
    assert 'id="experts-modal"' in INDEX
    for slot in ("experts-gallery", "experts-detail", "experts-review"):
        assert f'id="{slot}"' in INDEX, slot
    assert 'aria-label="Close experts"' in INDEX and 'id="close-experts-modal"' in INDEX
    assert "/static/js/experts.js" in INDEX


def test_the_mention_hook_is_registered_not_reimplemented():
    """experts.js hands rows to the one "@" popup; it does not grow a second."""
    assert "export function registerMentionSource" in MENTIONS
    assert "_extraRows(found.query)" in MENTIONS
    assert "registerMentionSource" in SRC and "from './fileMentions.js'" in SRC


def test_a_clearly_delimited_css_block_uses_theme_tokens_only():
    assert "/* ── Experts ──" in CSS
    for selector in (".exp-card", ".exp-card-counters", ".exp-file", ".exp-hit-ref",
                     ".exr-mark.is-accepted", ".exr-label.is-corpus", ".exr-label.is-opinion",
                     ".exr-card.is-high", ".exr-dropped", ".exr-result"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Experts ──", 1)[1]
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", block)
    assert not hexes, f"hardcoded hex colour in the Experts block: {hexes}"
    # No "#" at all, in fact: id selectors would also trip the Learned-rules
    # block's own guard, which reads everything after its marker.
    assert "#" not in block.split("*/", 1)[1], "the Experts block must be class-only"


# ── the gallery ────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_cards_carry_the_corpus_size_the_model_and_the_counters():
    out = _run(f"""
      const ROWS = {json.dumps(ROWS)};
      console.log(JSON.stringify({{
        html: expertsGalleryHtml(ROWS, {{}}),
        off: expertsGalleryHtml(ROWS, {{ enabled: false }}),
        loading: expertsGalleryHtml([], {{ loading: true }}),
        empty: expertsGalleryHtml([], {{}}),
        error: expertsGalleryHtml([], {{ error: 'HTTP 500 <x>' }}),
        nomatch: expertsGalleryHtml(ROWS, {{ query: 'zzz' }}),
      }}));
    """)
    html = out["html"]
    # name/description are escaped
    assert "Brenner &lt;b&gt;on craft&lt;/b&gt;" in html and "<b>on craft</b>" not in html
    # model, file count, chunk count, accepted/rejected counters
    assert ">llama3<" in html and "2 files" in html and "41 chunks" in html
    assert "✓ 5" in html and "✕ 1" in html
    assert ">auto<" in html  # an expert with no model of its own says so
    # one card per expert, opened and deleted by data attribute
    assert html.count('data-exp-open="') == 3 and 'data-exp-delete="lexicon"' in html
    assert "onclick=" not in html
    # a disabled expert is visibly off, not hidden
    assert "exp-card is-off" in html and ">off<" in html
    # the settings switch is reported, and it never blocks the editor
    assert "agent_experts" in out["off"] and "agent_experts" not in html
    # states
    assert "Loading experts" in out["loading"]
    assert "No experts yet" in out["empty"] and "data-exp-error hidden>" in out["empty"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-exp-error hidden>" not in out["error"]
    assert "No expert matches that search." in out["nomatch"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_sorting_and_filtering_are_deterministic():
    out = _run(f"""
      const ROWS = {json.dumps(ROWS)};
      console.log(JSON.stringify({{
        name: sortExperts(ROWS, 'name').map(r => r.slug),
        corpus: sortExperts(ROWS, 'corpus').map(r => r.slug),
        accepted: sortExperts(ROWS, 'accepted').map(r => r.slug),
        junk: sortExperts(ROWS, 'nonsense').map(r => r.slug),
        empty: sortExperts([], 'name'),
        find: filterExperts(ROWS, 'BREN').map(r => r.slug),
        byDesc: filterExperts(ROWS, 'word choice').map(r => r.slug),
        all: filterExperts(ROWS, '  ').map(r => r.slug),
        none: filterExperts(ROWS, 'nothing').length,
      }}));
    """)
    assert out["name"] == ["archivist", "brenner_bot", "lexicon"]
    assert out["corpus"] == ["brenner_bot", "lexicon", "archivist"]
    assert out["accepted"] == ["lexicon", "brenner_bot", "archivist"]
    assert out["junk"] == out["name"]          # an unknown mode sorts by name
    assert out["empty"] == []
    assert out["find"] == ["brenner_bot"] and out["byDesc"] == ["lexicon"]
    assert out["all"] == ["brenner_bot", "lexicon", "archivist"]  # blank query keeps order
    assert out["none"] == 0


# ── the editor ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_detail_edits_the_profile_and_lists_the_corpus_without_inventing_pages():
    out = _run(f"""
      const DETAIL = {json.dumps(DETAIL)};
      console.log(JSON.stringify({{
        html: expertDetailHtml(DETAIL, {{}}),
        saving: expertDetailHtml(DETAIL, {{ saving: true }}),
        bare: expertDetailHtml({{ expert: {{ slug: 'x' }} }}, {{}}),
        reindex: reindexSummaryHtml({{ indexed: 2, skipped: 1, removed: 0, chunks: 41, seconds: 0.1234 }}),
        junkReindex: reindexSummaryHtml(null) + reindexSummaryHtml({{}}),
      }}));
    """)
    html = out["html"]
    # every editable field is a named control
    for field in ("name", "description", "model", "temperature", "top_p", "enabled",
                  "instructions", "rubric"):
        assert f'data-exp-field="{field}"' in html, field
    assert "Read &lt;closely&gt;" in html and "<closely>" not in html
    # the rubric is one item per line in a textarea
    assert "no adverbs\ncut throat-clearing</textarea>" in html
    # corpus rows: size, pages, chunks, a link to the file and a delete button
    assert "2.3 MB" in html and "320 pages" in html and "40 chunks" in html
    assert 'href="/api/experts/brenner_bot/corpus/brenner.pdf"' in html
    assert "/corpus/notes%20%26%20more.md" in html      # the name is URL-encoded
    assert 'data-exp-file-delete="notes &amp; more.md"' in html
    # a file whose pages could not be determined says so — it never gets a number
    assert "pages unknown" in html
    # upload + reindex + search + block controls
    assert "data-exp-upload" in html and "data-exp-reindex" in html
    assert "data-exp-search-form" in html and "data-exp-block" in html
    assert "Show what the model sees" in html
    assert out["saving"].count("Saving…") == 1
    assert "data-exp-field=\"name\"" in out["bare"]      # a bare profile still renders
    # the reindex summary is the five numbers, inline
    for key in ("indexed", "skipped", "removed", "chunks"):
        assert key in out["reindex"], key
    assert "<b>2</b> indexed" in out["reindex"] and "<b>0.12s</b>" in out["reindex"]
    assert out["junkReindex"].count("exp-reindex-out") == 1   # null → nothing; {} → zeros
    assert "<b>0</b> indexed" in out["junkReindex"] and "NaN" not in out["junkReindex"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_search_hits_name_the_page_or_say_it_is_unknown():
    payload = {
        "query": "scene", "tier": "lexical", "degraded": True,
        "hits": [
            {"chunk_id": "c1", "source": "brenner.pdf", "page": 42, "text": "a <scene> turns",
             "score": 0.812},
            {"chunk_id": "c2", "source": "notes.md", "page": None, "text": "loose note",
             "score": 0.4},
        ],
    }
    out = _run(f"""
      const P = {json.dumps(payload)};
      console.log(JSON.stringify({{
        html: searchHitsHtml(P, 'brenner_bot'),
        none: searchHitsHtml({{ hits: [], query: 'ghost' }}, 'brenner_bot'),
        junk: searchHitsHtml(null, 'brenner_bot'),
        block: blockPreviewHtml({{ text: '[C1] brenner.pdf p.42\\n<x>', chunk_ids: ['c1'], chars: 26, budget: 2500 }}),
        emptyBlock: blockPreviewHtml({{ text: '' }}),
        label: pageLabelOf({{ page: 42 }}) + '|' + pageLabelOf({{ page: null }}) + '|'
               + pageLabelOf({{ page_label: 'page iv' }}) + '|' + pageLabelOf(null),
      }}));
    """)
    html = out["html"]
    assert "brenner.pdf, page 42" in html
    assert "notes.md, page unknown" in html and "notes.md, page 0" not in html
    assert 'href="/api/experts/brenner_bot/corpus/brenner.pdf"' in html
    assert "a &lt;scene&gt; turns" in html and "<scene>" not in html
    assert "0.812" in html
    # a missing semantic lane is reported, never as an error
    assert "Lexical only" in html
    assert "Nothing in this corpus matches" in out["none"] and "ghost" in out["none"]
    assert "Nothing in this corpus matches" in out["junk"]
    # the block preview shows the EXACT text with its budget
    assert "26 of 2500 chars" in out["block"] and "&lt;x&gt;" in out["block"]
    assert "the model would be given nothing" in out["emptyBlock"]
    assert out["label"] == "page 42|page unknown|page iv|page unknown"


# ── the review panel ───────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_every_correction_prints_its_label_verbatim_and_its_citation_ref():
    out = _run(f"""
      const R = {json.dumps(REVIEW)};
      console.log(JSON.stringify({{
        html: reviewPanelHtml(R, {{}}),
        decided: reviewPanelHtml(R, {{ decisions: {{ D1: 'accepted', D2: 'rejected' }} }}),
        unlabelled: deltaCardHtml({{ id: 'D9', op: 'EDIT', span: {{ start: 0, end: 1 }},
                                     quote: 'a', replacement: 'b', anchored: true }}, '', 'slug'),
      }}));
    """)
    html = out["html"]
    # the two labels, exactly as the backend wrote them
    assert '<span class="exr-label is-corpus">corpus</span>' in html
    assert "model&#39;s opinion, not the corpus" in html
    # …and never invented for a delta the result did not label, however anchored
    assert "no label in the result" in out["unlabelled"]
    assert ">corpus<" not in out["unlabelled"]
    # severity, rule, rationale, the before/after, the citation ref as a link
    assert 'class="exr-sev exr-sev-high">high<' in html and ">no filler<" in html
    assert "sharper &lt;verb&gt;" in html and "<verb>" not in html
    assert "<del class=\"exr-before\">quick</del>" in html
    assert '<ins class="exr-after">swift</ins>' in html
    assert 'href="/api/experts/brenner_bot/corpus/brenner.pdf"' in html
    assert ">brenner.pdf, page 42</a>" in html
    assert "notes.md, page unknown" in html
    # a KILL shows what goes, not an empty replacement box
    assert "exr-after" not in html.split('data-exr-card="D2"', 1)[1].split("</article>", 1)[0]
    # counts, the degraded warning, and the accept/reject pair per correction
    assert "<b>1</b> anchored to the corpus" in html and "<b>1</b> the model" in html
    assert "degraded" in html
    assert 'data-exr-accept="D1"' in html and 'data-exr-reject="D1"' in html
    assert "onclick=" not in html
    # decisions are reflected on the card, the mark and the counters
    decided = out["decided"]
    assert 'class="exr-btn exr-accept is-on" data-exr-accept="D1"' in decided
    assert "exr-card is-high is-accepted" in decided and "exr-card is-low is-rejected" in decided
    assert "<b>1</b> accepted · <b>1</b> rejected" in decided


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_original_text_is_marked_span_by_span_and_stays_escaped():
    out = _run("""
      const TEXT = 'The <b>quick</b> brown fox';
      const DELTAS = [
        { id: 'D2', op: 'ADD', span: { start: 26, end: 26 }, replacement: '!', severity: 'low' },
        { id: 'D1', op: 'EDIT', span: { start: 4, end: 16 }, quote: '<b>quick</b>',
          replacement: 'swift', severity: 'high', rule: 'no filler' },
        { id: 'D3', op: 'EDIT', span: { start: 900, end: 950 }, quote: 'gone' },
      ];
      console.log(JSON.stringify({
        plain: markedTextHtml(TEXT, DELTAS, {}),
        decided: markedTextHtml(TEXT, DELTAS, { D1: 'accepted' }),
        empty: markedTextHtml('', DELTAS, {}),
        junk: markedTextHtml(null, null, null),
      }));
    """)
    plain = out["plain"]
    assert "&lt;b&gt;quick&lt;/b&gt;" in plain and "<b>quick</b>" not in plain
    # the marks are in document order even though the deltas were not
    assert plain.index('data-exr-mark="D1"') < plain.index('data-exr-mark="D2"')
    # an insertion has no text of its own, so it gets a caret instead
    assert "exr-caret" in plain
    # an out-of-bounds span is skipped rather than corrupting the render
    assert 'data-exr-mark="D3"' not in plain
    assert "is-accepted" in out["decided"] and "is-accepted" not in plain
    assert out["empty"] == "" and out["junk"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_refused_corrections_are_listed_with_their_reason_not_swallowed():
    out = _run(f"""
      const R = {json.dumps(REVIEW)};
      const FLAT = {{ ...R, rejected: [{{ id: 'D4', op: 'ADD', reason: 'ADD has nothing to insert',
                                          quote: 'flat form' }}] }};
      console.log(JSON.stringify({{
        html: reviewPanelHtml(R, {{}}),
        flat: rejectedListHtml(FLAT.rejected),
        none: rejectedListHtml([]),
        junk: rejectedListHtml(null),
      }}));
    """)
    html = out["html"]
    assert "<details" in html and "1 correction the parser refused" in html
    assert "quote not found in the original text" in html
    # the quote comes from review()'s {"raw": {...}} and from compact_result()'s flat field
    assert "a ghost &lt;line&gt;" in html and "<line>" not in html
    assert "flat form" in out["flat"] and "ADD has nothing to insert" in out["flat"]
    assert out["none"] == "" and out["junk"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_panel_asks_for_what_it_does_not_have_instead_of_guessing():
    out = _run("""
      console.log(JSON.stringify({
        nothing: reviewPanelHtml(null, {}),
        noText: reviewPanelHtml({ expert: { slug: 's', name: 'S' },
                                  deltas: [{ id: 'D1', op: 'EDIT', span: { start: 0, end: 3 },
                                             quote: 'The', replacement: 'A' }] }, {}),
        counts: JSON.stringify(reviewCounts({ D1: 'accepted', D2: 'rejected', D3: 'accepted', D4: 'x' })),
        junkCounts: JSON.stringify(reviewCounts(null)),
        ids: acceptedIds([{ id: 'D1' }, { id: 'D2' }], { D2: 'accepted' }),
      }));
    """)
    assert "No review loaded" in out["nothing"] and "data-exr-paste" in out["nothing"]
    assert "does not carry the text" in out["noText"] and "data-exr-paste-text" in out["noText"]
    assert json.loads(out["counts"]) == {"accepted": 2, "rejected": 1}
    assert json.loads(out["junkCounts"]) == {"accepted": 0, "rejected": 0}
    assert out["ids"] == ["D2"]


# ── the contract with the backend ──────────────────────────────────────────

APPLY_TEXT = "The quick brown fox jumps over the lazy dog"
APPLY_DELTAS = [
    {"id": "D2", "op": "EDIT", "span": {"start": 20, "end": 25}, "quote": "jumps",
     "replacement": "vaults"},
    {"id": "D1", "op": "EDIT", "span": {"start": 4, "end": 9}, "quote": "quick",
     "replacement": "swift"},
    {"id": "D3", "op": "KILL", "span": {"start": 35, "end": 39}, "quote": "lazy",
     "replacement": "ignored — a KILL replaces with nothing"},
    {"id": "D4", "op": "ADD", "span": {"start": 43, "end": 43}, "replacement": "!"},
    {"id": "D5", "op": "EDIT", "span": {"start": 900, "end": 910}, "replacement": "out of bounds"},
    {"id": "D6", "op": "EDIT", "span": {"start": 9, "end": 4}, "replacement": "backwards"},
    {"id": "D7", "op": "EDIT", "span": {}, "replacement": "no span"},
    {"id": "D8", "op": "EDIT", "span": {"start": None, "end": 3}, "replacement": "null start"},
    {"id": "D9", "op": "EDIT", "span": {"start": "10", "end": "15"}, "replacement": "GREEN"},
]

APPLY_CASES = [
    ("all", None),
    ("some", ["D1", "D3"]),
    ("none", []),
    ("unknown-id", ["D1", "nope"]),
    ("string-offsets", ["D9"]),
    ("only-bad", ["D5", "D6", "D7", "D8"]),
    ("insert-and-kill", ["D3", "D4"]),
]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
@pytest.mark.parametrize("label,accept", APPLY_CASES, ids=[c[0] for c in APPLY_CASES])
def test_accepted_deltas_are_applied_exactly_as_the_backend_applies_them(label, accept):
    """Right to left, same skips, same result — or "Copy result" and the
    agent's `apply` action hand the user two different texts."""
    from src.expert_review import apply_deltas

    expected = apply_deltas(APPLY_TEXT, APPLY_DELTAS, accept)
    out = _run(f"""
      console.log(JSON.stringify({{ text: applyAcceptedDeltas(
        {json.dumps(APPLY_TEXT)}, {json.dumps(APPLY_DELTAS)}, {json.dumps(accept)}) }}));
    """)
    assert out["text"] == expected, label


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_junk_deltas_are_skipped_the_way_the_backend_skips_them():
    from src.expert_review import apply_deltas

    junk = [None, "nope", 7, {}, {"id": "D1", "op": "EDIT", "span": {"start": 0, "end": 3},
                                 "replacement": "One"}]
    expected = apply_deltas(APPLY_TEXT, [d for d in junk if isinstance(d, dict)], None)
    out = _run(f"""
      console.log(JSON.stringify({{
        text: applyAcceptedDeltas({json.dumps(APPLY_TEXT)}, {json.dumps(junk)}, null),
        noText: applyAcceptedDeltas(null, {json.dumps(junk)}, null),
        noDeltas: applyAcceptedDeltas({json.dumps(APPLY_TEXT)}, null, null),
      }}));
    """)
    assert out["text"] == expected
    assert out["noText"] == apply_deltas(None, junk, None)
    assert out["noDeltas"] == APPLY_TEXT


# ── defensive shapes ───────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_defensive_payload_unwrapping_and_field_defaults():
    out = _run("""
      console.log(JSON.stringify({
        wrapped: normalizeExperts({ experts: [{ slug: 'a' }, { slug: 'b' }] }).map(r => r.slug),
        bare: normalizeExperts([{ slug: 'a' }]).map(r => r.slug),
        nested: normalizeExperts({ data: { experts: [{ slug: 'a' }] } }).map(r => r.slug),
        junk: normalizeExperts(null),
        drops: normalizeExperts({ experts: [null, 'x', {}, { slug: 'ok' }] }).map(r => r.slug),
        defaults: normalizeExpert({ slug: 'a' }),
        badNums: normalizeExpert({ slug: 'a', chunks: 'x', accepted: null }),
        unwrapBare: unwrapExpert({ slug: 'a' }).slug,
        unwrapWrapped: unwrapExpert({ expert: { slug: 'b' } }).slug,
        unwrapJunk: unwrapExpert(null),
        detail: normalizeDetail({ expert: { slug: 's' } }),
        files: normalizeCorpusFiles({ files: [{ name: 'a.pdf', pages: 'x' }, null, { bytes: 1 }] }),
        rubricFromText: rubricLines('1. one\\n- two\\n\\n  three  '),
        rubricFromList: rubricLines(['a', '', ' b ']),
        rubricJunk: rubricLines(null),
        reviewWrapped: normalizeReview({ result: { deltas: [{ id: 'D1', op: 'edit',
                                                              span: { start: 0, end: 1 } }] } }).deltas.length,
        reviewCountsDerived: (() => { const r = normalizeReview({ deltas: [
            { id: 'D1', anchored: true, label: 'corpus' },
            { id: 'D2', anchored: false, label: 'x' }] });
          return [r.anchored_count, r.opinion_count]; })(),
        reviewJunk: normalizeReview(null).deltas.length,
        severity: normalizeDelta({ severity: 'CATASTROPHIC' }).severity,
        opUpper: normalizeDelta({ op: 'kill' }).op,
      }));
    """)
    assert out["wrapped"] == ["a", "b"] and out["bare"] == ["a"] and out["nested"] == ["a"]
    assert out["junk"] == [] and out["drops"] == ["ok"]
    assert out["defaults"]["enabled"] is True and out["defaults"]["chunks"] == 0
    assert out["defaults"]["name"] == "a"          # a nameless row falls back to its slug
    assert out["badNums"]["chunks"] == 0 and out["badNums"]["accepted"] == 0
    assert out["unwrapBare"] == "a" and out["unwrapWrapped"] == "b" and out["unwrapJunk"] == {}
    assert out["detail"]["expert"]["temperature"] == 0.2 and out["detail"]["expert"]["top_p"] == 1
    assert out["detail"]["files"] == [] and out["detail"]["usage"]["accepted"] == 0
    assert [f["name"] for f in out["files"]] == ["a.pdf"]
    assert out["files"][0]["pages"] is None        # an unreadable page count stays unknown
    assert out["rubricFromText"] == ["one", "two", "three"]
    assert out["rubricFromList"] == ["a", "b"] and out["rubricJunk"] == []
    assert out["reviewWrapped"] == 1
    assert out["reviewCountsDerived"] == [1, 1]    # counts derived only when absent
    assert out["reviewJunk"] == 0
    assert out["severity"] == "medium" and out["opUpper"] == "KILL"


# ── the composer mention ───────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_picking_an_expert_writes_the_expert_slug_token_into_the_composer():
    """The rows go to the one "@" popup, and what it inserts is
    `@expert:<slug> ` — the token the composer is meant to carry."""
    module = (REPO / "static/js/fileMentions.js").as_uri()
    out = _run(f"""
      import {{ applyPick }} from {json.dumps(module)};
      const ROWS = {json.dumps(ROWS)};
      const rows = expertMentionRows(ROWS, 'bren');
      console.log(JSON.stringify({{
        rows,
        all: expertMentionRows(ROWS, '').map(r => r.rel),
        bySlug: expertMentionRows(ROWS, 'lexi').map(r => r.rel),
        none: expertMentionRows(ROWS, 'zzz'),
        junk: expertMentionRows(null, 'a'),
        inserted: applyPick('ask @bren', 9, 4, rows[0].rel),
      }}));
    """)
    assert out["rows"][0]["rel"] == "expert:brenner_bot"
    assert out["rows"][0]["cat"] == "Experts"       # its own heading, not "Workspace files"
    # a disabled expert is not offered to the composer
    assert out["all"] == ["expert:brenner_bot", "expert:lexicon"]
    assert out["bySlug"] == ["expert:lexicon"]
    assert out["none"] == [] and out["junk"] == []
    assert out["inserted"]["value"] == "ask @expert:brenner_bot "
