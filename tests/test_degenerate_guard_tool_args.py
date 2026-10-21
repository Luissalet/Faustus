"""Tool-call arguments reach the degenerate-stream guard as raw JSON text.

21-10-2026, acceptance job on the Sparks: a plain SQLite schema written with
apply_patch ended 14 rounds in a row with "repeated phrase 'text not null n'
10 times". Every newline inside the argument string arrives escaped, and the
guard's word pattern read the letter after each backslash as a word, so
`TEXT NOT NULL,\\n` became the 4-gram 'text not null n' once per column. The
same schema as plain text never fired. The guard has to read the argument
text the way the tool will receive it, while still catching a real loop."""
import json
import re

import pytest

from src import llm_core
from tests.test_llm_core_streaming import _drive, _sse

SCHEMA = '''"""Storage for the agenda: one SQLite file, every write in one transaction."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    time_zone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    id TEXT NOT NULL PRIMARY KEY,
    resource TEXT NOT NULL,
    title TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    weekday TEXT NOT NULL,
    local_start TEXT NOT NULL,
    local_end TEXT NOT NULL,
    from_date TEXT NOT NULL,
    until_date TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fold INTEGER
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    resource TEXT NOT NULL REFERENCES resources(id),
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    status TEXT NOT NULL,
    series TEXT
);
"""
'''

# Token-like pieces, as a model stream delivers them: an escape on its own,
# then words, whitespace and punctuation.
_PIECE = re.compile(r'\\u[0-9a-fA-F]{4}|\\.|\w+|\s+|[^\w\s]')


def _argument_pieces(text: str) -> list:
    raw = json.dumps({"patch": "*** Begin Patch\n*** Add File: agenda/store.py\n" + text})
    return _PIECE.findall(raw)


def test_the_input_reproduces_the_live_failure_when_read_as_plain_words():
    guard = llm_core._DegenerateStreamGuard("glm")
    with pytest.raises(llm_core.DegenerateOutput) as exc:
        for piece in _argument_pieces(SCHEMA):
            guard.check(piece)
    assert "text not null n" in exc.value.reason


def test_a_schema_inside_tool_arguments_is_not_a_phrase_loop():
    guard = llm_core._DegenerateStreamGuard("glm")
    for piece in _argument_pieces(SCHEMA):
        guard.check(piece, json_escaped=True)
    assert "n" not in guard.recent_tokens


def test_an_escape_split_across_chunks_is_still_read_as_one_character():
    guard = llm_core._DegenerateStreamGuard("glm")
    for piece in ['"name TEXT', "\\", "nid TEXT", "\\u00", "f1o", "\\\\", "n"]:
        guard.check(piece, json_escaped=True)
    # "\\n" + "id" is a newline then "id"; "\\u00f1o" is "ño"; "\\\\" + "n" is a
    # literal backslash followed by the letter n, which is a real word. ("ñ"
    # is outside the guard's word alphabet, so "ño" counts as "o".)
    assert guard.recent_tokens == ["name", "text", "id", "text", "o", "n"]


def test_a_real_phrase_loop_inside_tool_arguments_is_still_cut():
    loop = "Also be a software developer mode? " * 40
    guard = llm_core._DegenerateStreamGuard("glm")
    with pytest.raises(llm_core.DegenerateOutput) as exc:
        for piece in _argument_pieces(loop):
            guard.check(piece, json_escaped=True)
    assert "repeated" in exc.value.reason


def test_a_loop_of_escaped_lines_is_still_cut():
    loop = "".join("    status TEXT NOT NULL,\n" for _ in range(60))
    guard = llm_core._DegenerateStreamGuard("glm")
    with pytest.raises(llm_core.DegenerateOutput):
        for piece in _argument_pieces(loop):
            guard.check(piece, json_escaped=True)


def test_the_stream_delivers_the_schema_patch_as_a_tool_call(monkeypatch):
    pieces = _argument_pieces(SCHEMA)
    lines = [_sse({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                   "function": {"name": "apply_patch", "arguments": ""}}]})]
    for piece in pieces:
        lines.append(_sse({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}))
    lines.append("data: [DONE]")
    events = _drive(monkeypatch, lines, model="glm-5.3-flash-nvfp4")
    errors = [e for e in events if e.get("error_class") == llm_core.DEGENERATE_OUTPUT_ERROR_CLASS]
    assert not errors, errors[:1]
    calls = [e["calls"] for e in events if e.get("type") == "tool_calls"]
    assert calls, events[-3:]
    assert calls[0][0]["name"] == "apply_patch"
    args = json.loads(calls[0][0]["arguments"])
    assert "until_date TEXT NOT NULL" in args["patch"]
