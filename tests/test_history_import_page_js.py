"""The Imported history page (static/js/historyImport.js): the import form
with its dry-run preview, the conversation list, the reader, and the two-tier
search over /api/history/*.

The renderers are pure (kept in a marked, dependency-free region of
historyImport.js) and run in node; the wiring is pinned at source level, like
the Experts and Learned-rules pages.

Three of these tests are the feature's honesty rules, not cosmetics:

  * a conversation with no date renders "date unknown". The store keeps an
    unreadable timestamp as NULL rather than stamping it with the import time,
    and the page may not undo that by printing today;
  * every skipped conversation is rendered WITH its reason — "6 skipped" on
    its own is a bug report the user cannot file; and
  * a result list always prints its tier, so ``degraded`` results are never
    served as if they were the best this machine can do.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/historyImport.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
INDEX = (REPO / "static/index.html").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── History: pure helpers"
PURE_END = "// ── History: end pure helpers ──"

ROWS = [
    {"id": "c1", "source": "chatgpt", "title": "Docker <b>GPU</b>", "model": "gpt-4o",
     "started_at": "2025-01-01T00:00:00Z", "message_count": 3},
    {"id": "c2", "source": "claude", "title": "Sourdough", "model": "",
     "started_at": "2026-01-02T10:00:00Z", "message_count": 2},
    # The case the whole feature turns on: a conversation the export gave no
    # usable date for.
    {"id": "c3", "source": "lmstudio", "title": "", "model": "",
     "started_at": None, "message_count": 5},
]

STATS = {"conversations": 3, "messages": 10, "oldest": "2025-01-01T00:00:00Z",
         "newest": "2026-01-02T10:00:00Z", "enabled": True,
         "sources": [{"source": "chatgpt", "conversations": 1, "messages": 3},
                     {"source": "claude", "conversations": 1, "messages": 2},
                     {"source": "lmstudio", "conversations": 1, "messages": 5}]}

PREVIEW = {"detected": "chatgpt", "files": 2, "conversations": 6, "messages": 13,
           "created": 4, "updated": 2, "seconds": 0.1234, "dry_run": True,
           "skipped": [{"why": "conversation has no mapping <of> message nodes",
                        "where": "conversations.json#conv-b"},
                       {"why": "conversation has no readable messages",
                        "where": "claude.json#cl-2"}]}

SEARCH = {
    "query": "nvidia", "tier": "lexical", "degraded": True, "candidates": 42,
    "elapsed_ms": 4.5,
    "hits": [
        # snippet_start 10, so the match at 24..30 is at offset 14 of the text
        {"message_id": "m1", "conversation_id": "c1", "title": "Docker GPU",
         "source": "chatgpt", "role": "user", "ts": "2025-01-01T00:00:00Z",
         "score": 0.81, "snippet": "I use the <nvidia> runtime here",
         "snippet_start": 10, "match_start": 20, "match_end": 28},
        # nothing literally matched: no highlight is invented
        {"message_id": "m2", "conversation_id": "c2", "title": "Sourdough",
         "source": "claude", "role": "assistant", "ts": None, "score": 0.2,
         "snippet": "feed it twice a day", "snippet_start": 0,
         "match_start": None, "match_end": None},
    ],
}

DETAIL = {"conversation": {
    "id": "c1", "source": "chatgpt", "title": "Docker GPU", "model": "gpt-4o",
    "started_at": "2025-01-01T00:00:00Z", "message_count": 3,
    "messages": [
        {"id": "m0", "role": "user", "content": "how do I <use> the runtime",
         "ts": "2025-01-01T00:00:00Z", "ordinal": 0},
        {"id": "m1", "role": "assistant", "content": "add a deploy block",
         "ts": "2025-01-01T00:01:00Z", "ordinal": 1},
        {"id": "m2", "role": "user", "content": "thanks", "ts": None, "ordinal": 2},
    ]}}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── source-level wiring ─────────────────────────────────────────────────────


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/historyImport.js")],
                          capture_output=True).returncode == 0
    # no inline handlers, no native dialogs (deletion goes through styledConfirm)
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "styledConfirm" in SRC and "data-his-delete" in SRC
    # every endpoint this page needs, all under the one request helper
    for path in ("/import", "/conversations?limit=", "/conversations/${encodeURIComponent(id)}",
                 "/search?q=", "/stats"):
        assert path in SRC, path
    assert "method: 'DELETE'" in SRC and "method: 'POST'" in SRC
    # the upload is multipart with the field name the route expects
    assert "new FormData()" in SRC and "form.append('file'" in SRC
    # exported entry points + the pure helpers the tests below drive
    for entry in ("export async function openHistoryPanel", "export function closeHistoryPanel",
                  "export async function loadHistory", "export function initHistoryImport"):
        assert entry in SRC, entry
    for fn in ("conversationListHtml", "conversationRowHtml", "conversationDetailHtml",
               "searchResultsHtml", "importFormHtml", "importPreviewHtml", "statsHtml",
               "highlightSnippet", "tierChipHtml", "dateLabel", "sourceLabel",
               "filterBySource", "normalizeConversations", "normalizeStats",
               "normalizeImport", "normalizeDetail", "normalizeSearch"):
        assert f"function {fn}" in SRC, fn
    # delegated listeners on the modal, not per-row handlers
    for kind in ("click", "submit", "change"):
        assert f"modal.addEventListener('{kind}'" in SRC, kind
    # errors land inline, not in a dialog — and are actually used
    assert "data-his-error" in SRC and "function inlineError" in SRC
    assert "data-his-import-error" in SRC and SRC.count("inlineError(") > 2


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


def test_the_page_has_an_entry_point_and_a_modal_shell():
    assert 'id="tool-history-btn"' in INDEX and ">Imported history</span>" in INDEX
    assert 'id="history-modal"' in INDEX
    for slot in ("history-main", "history-reader"):
        assert f'id="{slot}"' in INDEX, slot
    assert 'aria-label="Close imported history"' in INDEX
    assert 'id="close-history-modal"' in INDEX
    assert "/static/js/historyImport.js" in INDEX


def test_a_clearly_delimited_css_block_uses_theme_tokens_only():
    assert "/* ── Imported history ──" in CSS
    for selector in (".his-import", ".his-preview", ".his-skipped", ".his-row",
                     ".his-tier", ".his-degraded", ".his-mark", ".his-msg",
                     ".his-btn", ".his-empty"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Imported history ──", 1)[1].split("*/", 1)[1]
    # No "#" at all — not a hex colour and not an id selector. Both would ALSO
    # trip the Learned-rules and Experts blocks' own guards, which read
    # everything after their marker to the end of the file.
    assert "#" not in block, "the block must be class-only, with no literal colours"
    assert re.search(r"var\(--", block), "it paints with theme tokens or not at all"
    # the button classes that paint themselves define their own :hover, which
    # is what tests/test_css_guardrails.py insists on
    assert ".his-btn:hover:not(:disabled)" in CSS
    assert ".his-btn.is-primary:hover:not(:disabled)" in CSS


# ── the import form and its preview ─────────────────────────────────────────


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_form_offers_a_path_and_a_file_and_previews_before_importing():
    out = _run("""
      console.log(JSON.stringify({
        idle: importFormHtml({}),
        busy: importFormHtml({ busy: true, path: '/home/me/export' }),
        error: importFormHtml({ error: 'no such file <x>' }),
        junk: importFormHtml(null),
      }));
    """)
    idle = out["idle"]
    assert "data-his-path" in idle and "data-his-file" in idle and "data-his-source" in idle
    assert 'type="file"' in idle and 'accept=".json,application/json"' in idle
    # the first button is a PREVIEW, never a straight import
    assert "Preview import" in idle and "data-his-import-form" in idle
    assert "Import them" not in idle
    # every known source is offered, plus automatic detection
    for label in ("ChatGPT", "Claude", "LM Studio", "Faustus", "detect automatically"):
        assert label in idle, label
    assert out["busy"].count("disabled") >= 4 and "Reading…" in out["busy"]
    assert "/home/me/export" in out["busy"]
    assert "no such file &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-his-import-error hidden" in idle
    assert "data-his-import-error hidden" not in out["error"]
    assert "data-his-path" in out["junk"], "a null state still renders the form"


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_every_skipped_conversation_is_shown_with_its_reason():
    out = _run(f"""
      const P = {json.dumps(PREVIEW)};
      console.log(JSON.stringify({{
        dry: importPreviewHtml(P),
        done: importPreviewHtml({{ ...P, dry_run: false }}),
        clean: importPreviewHtml({{ ...P, skipped: [] }}),
        empty: importPreviewHtml({{ ...P, conversations: 0 }}),
        junk: importPreviewHtml(null) + importPreviewHtml({{}}),
      }}));
    """)
    dry = out["dry"]
    # the counts
    assert "<b>6</b>" in dry and "<b>13</b>" in dry
    assert "<b>4</b> new" in dry and "<b>2</b> already here" in dry
    assert "0.12s" in dry and "ChatGPT" in dry and "would import" in dry
    # …and the reason for EVERY skip, escaped
    assert "2 skipped" in dry
    assert "conversations.json#conv-b" in dry and "claude.json#cl-2" in dry
    assert "no mapping &lt;of&gt; message nodes" in dry and "<of>" not in dry
    assert "conversation has no readable messages" in dry
    # the commit button only exists on a dry run that found something
    assert "data-his-commit" in dry and "data-his-cancel" in dry
    assert "data-his-commit" not in out["done"] and "imported" in out["done"]
    assert "data-his-commit" not in out["empty"]
    assert "Nothing was skipped." in out["clean"]
    assert out["junk"].count('class="his-preview"') == 1   # null → nothing; {} → zeros
    assert "NaN" not in out["junk"] and "nothing recognised" in out["junk"]


# ── the list ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_conversation_with_no_date_says_so_and_never_gets_todays():
    out = _run(f"""
      const ROWS = {json.dumps(ROWS)};
      const STATS = {json.dumps(STATS)};
      console.log(JSON.stringify({{
        html: conversationListHtml(ROWS, {{ stats: STATS }}),
        off: conversationListHtml(ROWS, {{ stats: STATS, enabled: false }}),
        loading: conversationListHtml([], {{ loading: true }}),
        empty: conversationListHtml([], {{}}),
        error: conversationListHtml(ROWS, {{ error: 'HTTP 500 <x>' }}),
        filtered: conversationListHtml(ROWS, {{ source: 'claude' }}),
        nomatch: conversationListHtml(ROWS, {{ source: 'faustus' }}),
        junk: conversationListHtml(null, null),
        labels: [dateLabel('2025-01-01T00:00:00Z'), dateLabel(null), dateLabel(''),
                 dateLabel('not a date'), dateLabel(0)].join('|'),
        sources: [sourceLabel('chatgpt'), sourceLabel('lmstudio'),
                  sourceLabel('weird'), sourceLabel(null)].join('|'),
      }}));
    """)
    html = out["html"]
    # the honesty rule
    assert "date unknown" in html
    assert out["labels"] == "1 Jan 2025|date unknown|date unknown|date unknown|date unknown"
    assert out["sources"] == "ChatGPT|LM Studio|weird|unknown"
    # titles are escaped, an empty title reads as Untitled
    assert "Docker &lt;b&gt;GPU&lt;/b&gt;" in html and "<b>GPU</b>" not in html
    assert "Untitled" in html
    # one row per conversation, opened and deleted by data attribute
    assert html.count('data-his-open="') == 3 and 'data-his-delete="c2"' in html
    assert "onclick=" not in html
    # counters and the source filter
    assert "<b>3</b> conversations" in html and "<b>10</b> messages" in html
    assert "1 Jan 2025 – 2 Jan 2026" in html
    assert "data-his-filter" in html and "data-his-search-form" in html
    # states
    assert "Loading imported history" in out["loading"]
    assert "Nothing imported yet." in out["empty"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-his-error hidden" in html and "data-his-error hidden" not in out["error"]
    assert out["filtered"].count('data-his-open="') == 1
    assert "No conversation from that source." in out["nomatch"]
    assert "his-list" not in out["junk"] and "NaN" not in out["junk"]
    # the settings switch is reported and never blocks the page
    assert "agent_history_import" in out["off"] and "agent_history_import" not in html


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_filtering_by_source_is_deterministic():
    out = _run(f"""
      const ROWS = {json.dumps(ROWS)};
      console.log(JSON.stringify({{
        all: filterBySource(ROWS, '').map(r => r.id),
        blank: filterBySource(ROWS, '   ').map(r => r.id),
        every: filterBySource(ROWS, 'all').map(r => r.id),
        one: filterBySource(ROWS, 'claude').map(r => r.id),
        upper: filterBySource(ROWS, 'CLAUDE').map(r => r.id),
        none: filterBySource(ROWS, 'nothing').length,
        junk: filterBySource(null, 'claude').length,
      }}));
    """)
    assert out["all"] == out["blank"] == out["every"] == ["c1", "c2", "c3"]
    assert out["one"] == out["upper"] == ["c2"]
    assert out["none"] == 0 and out["junk"] == 0


# ── search ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_results_print_their_tier_and_mark_only_what_actually_matched():
    out = _run(f"""
      const S = {json.dumps(SEARCH)};
      console.log(JSON.stringify({{
        html: searchResultsHtml(S),
        refined: searchResultsHtml({{ ...S, tier: 'refined', degraded: false }}),
        none: searchResultsHtml({{ ...S, hits: [], query: 'gh<o>st' }}),
        junk: searchResultsHtml(null) + searchResultsHtml({{}}),
        marks: [
          highlightSnippet(S.hits[0]),
          highlightSnippet(S.hits[1]),
          highlightSnippet({{ snippet: 'abc', snippet_start: 0, match_start: 5, match_end: 9 }}),
          highlightSnippet({{ snippet: 'abc', snippet_start: 0, match_start: 2, match_end: 1 }}),
          highlightSnippet(null),
        ],
      }}));
    """)
    html = out["html"]
    # the tier, always
    assert "tier: lexical" in html and "degraded" in html
    assert "tier: refined" in out["refined"] and "his-degraded" not in out["refined"]
    assert "2 of 42 candidates" in html
    # the highlight is the span the backend found, at its real offset
    assert "<mark class=\"his-mark\">&lt;nvidia&gt;</mark>" in out["marks"][0]
    assert out["marks"][0].endswith("</mark> runtime here")
    assert out["marks"][0].startswith("I use the <mark")
    # a hit with no match is printed plain — no highlight is invented
    assert "<mark" not in out["marks"][1] and out["marks"][1] == "feed it twice a day"
    # offsets that cannot be trusted degrade to plain text rather than slicing junk
    assert out["marks"][2] == "abc" and out["marks"][3] == "abc"
    assert out["marks"][4] == ""
    # a hit with no timestamp still says so rather than inventing one
    assert "date unknown" in html
    assert 'data-his-open="c1"' in html
    assert "Nothing matched “gh&lt;o&gt;st”." in out["none"]
    assert "<o>" not in out["none"]
    assert out["junk"] == "", "no payload renders nothing at all"


# ── the reader ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_reader_shows_every_message_in_order_and_escapes_them():
    out = _run(f"""
      const D = {json.dumps(DETAIL)};
      console.log(JSON.stringify({{
        html: conversationDetailHtml(D),
        bare: conversationDetailHtml({{ conversation: {{ id: 'x' }} }}),
        junk: conversationDetailHtml(null),
      }}));
    """)
    html = out["html"]
    assert html.index("how do I") < html.index("add a deploy") < html.index("thanks")
    assert "how do I &lt;use&gt; the runtime" in html and "<use>" not in html
    assert 'class="his-msg is-user"' in html and 'class="his-msg is-assistant"' in html
    assert "data-his-back" in html
    assert "ChatGPT" in html and "gpt-4o" in html and "3 messages" in html
    # the message with no timestamp says so
    assert html.count("date unknown") == 1
    assert "This conversation has no messages." in out["bare"]
    assert "Untitled" in out["junk"] and "NaN" not in out["junk"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_normalisers_survive_every_wrapper_and_every_junk_payload():
    out = _run("""
      const shapes = [
        null, undefined, 3, 'text', [],
        { conversations: [{ id: 'a', source: 'claude' }] },
        [{ id: 'b' }],
        { items: [{ id: 'c' }] },
        { data: { conversations: [{ id: 'd' }] } },
        { conversations: [null, 3, { no_id: 1 }, { id: '' }, { id: 'e' }] },
      ];
      console.log(JSON.stringify({
        counts: shapes.map(s => normalizeConversations(s).length),
        ids: normalizeConversations({ conversations: [{ id: 'z' }] }).map(r => r.id),
        nullDate: normalizeConversations({ conversations: [{ id: 'z' }] })[0].started_at,
        stats: [normalizeStats(null), normalizeStats({ stats: { conversations: 'x' } }),
                normalizeStats({ conversations: 4, sources: 'nope' })],
        imp: [normalizeImport(null), normalizeImport({ skipped: [null, 3, {}] })],
        det: normalizeDetail({ conversation: { id: 'q', messages: [null, { role: 'x' }] } }),
        search: [normalizeSearch(null), normalizeSearch({ hits: [null, { score: 'x' }] })],
      }));
    """)
    assert out["counts"] == [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
    assert out["ids"] == ["z"]
    assert out["nullDate"] is None, "a missing date is null, never a fabricated string"
    assert out["stats"][0]["conversations"] == 0 and out["stats"][0]["sources"] == []
    assert out["stats"][1]["conversations"] == 0     # "x" is not a number
    assert out["stats"][2]["sources"] == []
    assert out["imp"][0]["conversations"] == 0 and out["imp"][0]["skipped"] == []
    assert out["imp"][1]["skipped"] == [{"why": "no reason given", "where": ""}]
    assert len(out["det"]["messages"]) == 1 and out["det"]["messages"][0]["role"] == "x"
    assert out["search"][0]["hits"] == [] and out["search"][0]["tier"] == "lexical"
    assert out["search"][1]["hits"][0]["score"] == 0
    assert out["search"][1]["hits"][0]["match_start"] is None
