"""Lote 88 (seen live): "usa la herramienta git_status" got "no tengo
git_status" because tool retrieval never ranked it. Two rules now hold in
`src/agent_loop.py`: a tool NAMED in the request is offered, and a workspace
with a git repository offers the read-only git tools (all of them when the
request talks git)."""
from __future__ import annotations

import re

from src import agent_loop


def _block():
    src = open(agent_loop.__file__, encoding="utf-8").read()
    start = src.index("Tools the user NAMED are offered")
    return src[start:start + 3500]


def test_named_tools_are_offered_and_git_floor_exists():
    block = _block()
    assert "TOOL_HANDLERS as _all_tool_names" in block
    assert "_GIT_TOOL_NAMES" in block
    assert '"git_status", "git_log", "git_diff"' in block
    # the mutating set only arrives with git intent and never on a vague turn
    assert "_git_intent and not _low_signal_turn" in block


def test_git_intent_regex_matches_spanish_and_english():
    block = _block()
    m = re.search(r're\.search\(\s*r"(.+?)"\s*r"(.+?)",', block, re.S)
    assert m, "intent regex not found"
    pattern = m.group(1) + m.group(2)
    rx = re.compile(pattern, re.IGNORECASE)
    for text in ("haz commit y push", "crea una rama nueva", "what branch am I on", "sube el repo"):
        assert rx.search(text), text
    assert not rx.search("resume este documento")
