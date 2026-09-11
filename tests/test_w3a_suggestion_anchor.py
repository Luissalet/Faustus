"""tests/test_w3a_suggestion_anchor.py — W3-A (CONTRATO_W3.md, ref CONTRATO_CMP_W2.md).

`suggest_document` (`src/agent_tools/document_tools.py`) now attaches an
`anchor {quote, before, after}` to every valid suggestion, computed from the
CURRENT document content with `src/document_comments.py`'s own
`locate_quote`/`context_for` helpers — never a second, diverging
implementation:

* `find` unique in the document -> the anchor carries its REAL surrounding
  text (same `_CTX_CHARS` window a comment's anchor gets), so a client can
  apply the suggestion without asking.
* `find` repeated -> the anchor carries the bare quote with empty
  `before`/`after` — the server never guesses WHICH occurrence the model
  meant; that stays the client's existing occurrence picker's job.
"""
import asyncio
import sys
import types

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools.document_tools import set_active_document
from src.document_comments import context_for


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return (self.name, "eq", value)

    def desc(self):
        return (self.name, "desc")


class _Document:
    id = _Column("id")
    owner = _Column("owner")
    is_active = _Column("is_active")
    updated_at = _Column("updated_at")


class _Doc:
    def __init__(self, doc_id, content):
        self.id = doc_id
        self.current_content = content
        self.version_count = 1


class _Query:
    def __init__(self, doc):
        self.filters = []
        self.doc = doc

    def filter(self, *clauses):
        self.filters.extend(clauses)
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self

    def all(self):
        return [self.doc] if self.doc else []

    def first(self):
        return self.doc


class _Db:
    def __init__(self, query):
        self.query_obj = query

    def query(self, *args):
        return self.query_obj

    def close(self):
        pass


def _install(monkeypatch, doc):
    query = _Query(doc)
    db = _Db(query)
    db_mod = types.ModuleType("src.database")
    db_mod.SessionLocal = lambda: db
    db_mod.Document = _Document
    monkeypatch.setitem(sys.modules, "src.database", db_mod)
    return db


def _suggest(monkeypatch, doc, find, replace="replacement", reason="a reason"):
    _install(monkeypatch, doc)
    set_active_document(doc.id)
    try:
        block = f"<<<FIND>>>\n{find}\n<<<SUGGEST>>>\n{replace}\n<<<REASON>>>\n{reason}\n<<<END>>>"
        return asyncio.run(
            TOOL_HANDLERS["suggest_document"](block, {"owner": "alice", "doc_id": doc.id})
        )
    finally:
        set_active_document(None)


def test_anchor_unique_find_carries_real_surrounding_context(monkeypatch):
    content = "Intro paragraph here.\n\nThe quick brown fox jumps.\n\nOutro line."
    quote = "The quick brown fox jumps."
    result = _suggest(monkeypatch, _Doc("doc-1", content), quote)

    assert result["action"] == "suggest"
    assert result["count"] == 1
    [sugg] = result["suggestions"]
    assert "anchor" in sugg, "a suggestion whose find is unique must carry an anchor"
    anchor = sugg["anchor"]
    assert anchor["quote"] == quote

    start = content.index(quote)
    end = start + len(quote)
    expected_before, expected_after = context_for(content, start, end)
    assert anchor["before"] == expected_before
    assert anchor["after"] == expected_after
    # This document has real text on both sides — a degenerate empty-context
    # anchor here would silently defeat the whole point of the feature.
    assert anchor["before"]
    assert anchor["after"]


def test_anchor_repeated_find_carries_no_context_never_guesses(monkeypatch):
    content = "Repeat me. Filler in between. Repeat me. More filler after."
    quote = "Repeat me."
    result = _suggest(monkeypatch, _Doc("doc-2", content), quote)

    [sugg] = result["suggestions"]
    anchor = sugg["anchor"]
    assert anchor["quote"] == quote
    # Ambiguous in the document -> the server attaches no positional context
    # at all, rather than picking one occurrence's surroundings and letting
    # the client silently apply at the wrong one.
    assert anchor["before"] == ""
    assert anchor["after"] == ""


def test_anchor_present_for_every_valid_suggestion_in_a_batch(monkeypatch):
    content = "Alpha section.\n\nBeta section.\n\nGamma section."
    doc = _Doc("doc-3", content)
    _install(monkeypatch, doc)
    set_active_document(doc.id)
    try:
        block = (
            "<<<FIND>>>\nAlpha section.\n<<<SUGGEST>>>\nAlpha part.\n<<<REASON>>>\nr1\n<<<END>>>\n"
            "<<<FIND>>>\nGamma section.\n<<<SUGGEST>>>\nGamma part.\n<<<REASON>>>\nr2\n<<<END>>>"
        )
        result = asyncio.run(
            TOOL_HANDLERS["suggest_document"](block, {"owner": "alice", "doc_id": doc.id})
        )
    finally:
        set_active_document(None)

    assert result["count"] == 2
    for sugg in result["suggestions"]:
        assert sugg["anchor"]["quote"] == sugg["find"]
        # Both finds are unique in this document -> both get real context.
        assert sugg["anchor"]["before"] or sugg["anchor"]["after"]
