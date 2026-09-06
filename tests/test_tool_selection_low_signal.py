"""A "low-signal" turn may still name the tool it needs.

Seen live (07-09-2026). Luis asked a chat in a project to write the report's
objectives into the project, got them as prose, and said:

    "I meant pu them into the project objectives tab of the project"

The intent classifier scored that as `low_signal=True, domains=[]` — no verb it
recognises, no domain — and the tool-selection branch for "low signal **and** a
workspace is bound" ASSIGNED a fixed read-only file toolset into
`_relevant_tools`, which short-circuits the retrieval below it. So the model was
offered eleven tools, none of them `project_objectives`, and answered — truthfully
— that it had no such tool. To the person looking at the Objectives tab, that is
the app lying about itself.

The retrieval was never the problem: ask the index for that exact sentence and
`project_objectives` comes back (the test below does). The bug was that nobody
asked. A floor has to be a floor, not a ceiling — which the no-workspace branch
right next to it already said in a comment.
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LOOP = (_REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")

_MESSAGE = "I meant pu them into the project objectives tab of the project"


def _low_signal_branch() -> str:
    start = _LOOP.index("if not guide_only and not _relevant_tools and _low_signal_turn:")
    end = _LOOP.index("if not guide_only and not _relevant_tools:", start)
    return _LOOP[start:end]


def test_the_low_signal_branch_does_not_short_circuit_retrieval():
    """It may raise the floor; it may not decide the selection."""
    branch = _low_signal_branch()
    assert "_low_signal_readonly_floor" in branch
    # The whole bug in one line: any assignment to _relevant_tools here means
    # the `if not _relevant_tools` retrieval below is skipped.
    assert not re.search(r"^\s*_relevant_tools\s*(?:\|)?=", branch, re.M), branch


def test_the_floor_is_applied_after_the_retrieval():
    """Order matters: retrieve first, then union the floor, never replace."""
    floor_set = _LOOP.index("_low_signal_readonly_floor = set(ALWAYS_AVAILABLE)")
    retrieval = _LOOP.index("tool_idx.get_tools_for_query", floor_set)
    union = _LOOP.index("_relevant_tools = set(_relevant_tools) | _low_signal_readonly_floor", retrieval)
    assert floor_set < retrieval < union


def test_the_read_only_half_is_still_read_only():
    """The floor still holds back the write/shell half on a vague turn."""
    branch = _low_signal_branch()
    assert "PLAN_MODE_READONLY_TOOLS" in branch


@pytest.mark.skipif(
    __import__("os").environ.get("FAUSTUS_SKIP_TOOL_INDEX") == "1",
    reason="tool index not wanted here",
)
def test_the_index_had_the_answer_all_along():
    """The retrieval that was skipped returns the tool the sentence names.

    Skipped when the index cannot be built (no embedding backend in this
    environment) — the point is the wiring above, and this is the evidence
    that the wiring had something to deliver."""
    try:
        from src.tool_index import get_tool_index, ALWAYS_AVAILABLE
        index = get_tool_index()
    except Exception as exc:                      # pragma: no cover - env dependent
        pytest.skip(f"tool index unavailable: {exc}")
    if not index:                                 # pragma: no cover - env dependent
        pytest.skip("tool index unavailable")
    try:
        tools = index.get_tools_for_query(_MESSAGE, 8)
    except Exception as exc:                      # pragma: no cover - env dependent
        pytest.skip(f"retrieval unavailable: {exc}")
    assert "project_objectives" in set(tools) - set(ALWAYS_AVAILABLE)
