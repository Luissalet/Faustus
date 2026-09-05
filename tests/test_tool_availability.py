"""B-007: offered, then refused — and what happens next.

`suggest_document` could appear in a turn's tool list and answer "No active
document to suggest on" when called. The audit asks for three things and each
has a test here:

  1. ONE availability function, evaluated at the point of use;
  2. an explainable state transition when a tool stops being valid;
  3. the tool set updated for the next round — including back again when the
     context changes DURING the turn, which is why this cannot live in the
     preflight.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src import tool_availability as availability
from src.agent_tools import document_tools


# ── 1. one function, asked at the point of use ─────────────────────────────

def test_a_tool_with_no_rule_is_available():
    verdict = availability.evaluate("web_search", {})
    assert verdict.available is True
    assert verdict.reason == ""


def test_suggest_document_needs_a_document():
    blocked = availability.evaluate("suggest_document", {})
    assert blocked.available is False
    assert blocked.reason == "No active document to suggest on"
    assert "create_document" in blocked.remedy
    assert blocked.restored_by == frozenset({"create_document", "manage_documents"})


@pytest.mark.parametrize("ctx", [
    {"doc_id": "doc-1"},                       # the call names its own target
    {"active_document_id": "doc-2"},           # the editor has one open
    {"doc_id": "doc-1", "active_document_id": "doc-2"},
])
def test_suggest_document_is_available_with_a_target(ctx):
    assert availability.evaluate("suggest_document", ctx).available is True


def test_a_broken_rule_does_not_block_the_tool(monkeypatch):
    """A bug in this module must not take a working tool off the table."""
    def explode(_ctx):
        raise RuntimeError("rule bug")

    rule = availability._RULES["suggest_document"]
    monkeypatch.setitem(
        availability._RULES, "suggest_document",
        availability._Rule(blocked_because=explode, remedy=rule.remedy,
                           restored_by=rule.restored_by),
    )
    assert availability.evaluate("suggest_document", {}).available is True


def test_evaluate_never_raises_on_junk():
    for junk in (None, 123, object(), ""):
        assert availability.evaluate(junk, None).available is True
    assert availability.evaluate("suggest_document", None).available is False


# ── 2. an explainable refusal, not a bare error ────────────────────────────

def test_the_refusal_says_what_it_is_and_what_would_fix_it():
    result = availability.unavailable_result("suggest_document", {})
    assert result["error"] == "No active document to suggest on"  # old wording kept
    assert result[availability.UNAVAILABLE_KEY] is True
    assert result["tool"] == "suggest_document"
    assert result["state"] == "unavailable"
    assert result["retry"] is False
    assert result["restored_by"] == ["create_document", "manage_documents"]
    assert availability.is_unavailable(result)
    assert availability.withdrawn_tool(result) == "suggest_document"


def test_an_ordinary_error_is_not_an_unavailability():
    assert not availability.is_unavailable({"error": "boom"})
    assert not availability.is_unavailable({"output": "fine"})
    assert not availability.is_unavailable(None)
    assert availability.withdrawn_tool({"error": "boom"}) == ""


def test_available_tools_produce_no_refusal():
    assert availability.unavailable_result("suggest_document", {"doc_id": "d"}) is None


def test_the_tool_itself_returns_the_refusal(monkeypatch):
    """The point of use, for real: the tool, with nothing open."""
    monkeypatch.setattr(document_tools, "_active_document_id", None)
    result = asyncio.run(
        document_tools.SuggestDocumentTool().execute("anything", {"owner": "luis"})
    )
    assert availability.is_unavailable(result)
    assert result["tool"] == "suggest_document"
    assert result["retry"] is False


def test_the_tool_runs_when_the_editor_has_a_document(monkeypatch):
    """With a document open the refusal is gone — whatever happens next is
    the tool's own business, not an availability answer."""
    monkeypatch.setattr(document_tools, "_active_document_id", "doc-42")
    result = asyncio.run(
        document_tools.SuggestDocumentTool().execute("no blocks here", {"owner": "luis"})
    )
    assert not availability.is_unavailable(result)


# ── 3. the next round's tool set, in both directions ───────────────────────

def _round_bookkeeping(relevant, base, withdrawn, block_tool, result):
    """The loop's bookkeeping, isolated from the 8k-line generator it lives in.

    Mirrors src/agent_loop.py: withdraw on a refusal, restore when a tool that
    supplies the missing thing succeeds.
    """
    name = availability.withdrawn_tool(result)
    if name and relevant is not None:
        relevant.discard(name)
        if base is not None:
            base.discard(name)
        withdrawn[name] = str(result.get("reason") or "")
    elif withdrawn and not result.get("error"):
        for tool in list(withdrawn):
            if block_tool in availability.restored_by(tool):
                withdrawn.pop(tool, None)
                if relevant is not None:
                    relevant.add(tool)
                    if base is not None:
                        base.add(tool)
    return relevant, withdrawn


def test_a_refusal_takes_the_tool_out_of_the_next_round():
    tools = {"suggest_document", "web_search"}
    withdrawn = {}
    refusal = availability.unavailable_result("suggest_document", {})
    tools, withdrawn = _round_bookkeeping(tools, None, withdrawn,
                                          "suggest_document", refusal)
    assert "suggest_document" not in tools
    assert withdrawn["suggest_document"] == "No active document to suggest on"
    assert "web_search" in tools  # nothing else is touched


def test_the_context_changing_mid_turn_brings_the_tool_back():
    """The scenario the preflight cannot handle.

    Round 1: nothing is open, suggest_document refuses and is withdrawn.
    Round 2: create_document succeeds — the document now exists.
    Round 3: suggest_document is offered again, and this time it can run.
    """
    tools = {"suggest_document", "create_document"}
    base = set(tools)
    withdrawn = {}

    tools, withdrawn = _round_bookkeeping(
        tools, base, withdrawn, "suggest_document",
        availability.unavailable_result("suggest_document", {}),
    )
    assert "suggest_document" not in tools and "suggest_document" not in base

    tools, withdrawn = _round_bookkeeping(
        tools, base, withdrawn, "create_document",
        {"output": "created", "document_id": "doc-9"},
    )
    assert "suggest_document" in tools and "suggest_document" in base
    assert withdrawn == {}

    # ...and with the document open it really is available now.
    assert availability.evaluate(
        "suggest_document", {"active_document_id": "doc-9"}
    ).available is True


def test_an_unrelated_success_does_not_restore_it():
    tools, withdrawn = {"web_search"}, {}
    tools, withdrawn = _round_bookkeeping(
        tools, None, withdrawn, "suggest_document",
        availability.unavailable_result("suggest_document", {}),
    )
    tools, withdrawn = _round_bookkeeping(
        tools, None, withdrawn, "web_search", {"output": "results"},
    )
    assert "suggest_document" not in tools
    assert "suggest_document" in withdrawn


def test_a_failed_opener_does_not_restore_it():
    tools, withdrawn = set(), {}
    tools, withdrawn = _round_bookkeeping(
        tools, None, withdrawn, "suggest_document",
        availability.unavailable_result("suggest_document", {}),
    )
    tools, withdrawn = _round_bookkeeping(
        tools, None, withdrawn, "create_document", {"error": "disk full"},
    )
    assert "suggest_document" not in tools
    assert "suggest_document" in withdrawn


def test_the_loop_is_actually_wired_to_this():
    """A guard: the bookkeeping above is only true if agent_loop does it."""
    from pathlib import Path
    source = Path("src/agent_loop.py").read_text(encoding="utf-8")
    assert "tool_availability" in source
    assert "withdrawn_tool(result)" in source
    assert "restored_by(_name)" in source


def test_the_conditional_tools_are_listed():
    assert availability.conditional_tools() == ("suggest_document",)
