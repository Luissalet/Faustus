"""S1.2: `docs/api/sse_events.json` is the SDK's contract for what a `type`
can arrive on `POST /api/chat_stream` / `GET /api/chat/resume/{session_id}`.
This is the guard that keeps it honest: it re-scans the source for every
`type` literal that reaches the wire and fails the moment one is not in the
catalogue, so a client generated from the JSON cannot be silently outdated.

Source scope, and why: the catalogue's own description names two routes;
`routes/chat_routes.py` is where both live, `src/agent_loop.py` is the agent
turn that route streams, `src/agent_runs.py` is the envelope/replay layer
every frame passes through, and `src/chat_outbox.py` is the idempotency
store chat_stream reads before either of those run. Two more modules were
found by tracing what those files themselves forward verbatim onto the
stream (`grep -n "async for .* in .*:$" src/agent_loop.py routes/chat_routes.py`,
by hand, once — not re-run by this guard, since a hidden new delegate would
by definition not be listed here for the guard to check): `src/llm_core.py`
(`stream_llm_with_fallback`'s raw provider frames — `finish`, `usage`,
`tool_calls`, `tool_call_delta`, `model_actual`, `fallback` — chat_routes.py
forwards these with a bare `yield chunk` in "chat" mode) and
`src/teacher_escalation.py` (`run_teacher_inline`'s frames — agent_loop.py's
`async for evt in run_teacher_inline(...): yield evt`). Anything reachable
through a FUTURE such delegation this scan did not anticipate is exactly
what test (a) below exists to catch: it fails loud instead of silently
passing, and the fix is a one-line addition to `_SOURCE_FILES` (if a new
module needs scanning) — being honest about that limit is the point of
writing it down here instead of implying this list is exhaustive by
construction.

Not every `"type": "..."` in these files is an SSE frame: model messages and
tool-call payloads use the same key name for provider content-block shapes
(`{"type": "text", ...}`, `{"type": "image_url", ...}`, a native tool-call
block, a native tool-result block, a `base64` image source, `ephemeral`
prompt-cache control, a function/tool definition block, a model-refusal
content block, a JSON-schema `object` block). Those are excluded below, by
literal, with the shape that put each one there — the same discipline
`event_types()`/`core_event_types()` already apply by only counting the
catalogue's own `event.type` key, not a substring.
"""
from __future__ import annotations

import re
from pathlib import Path

from src import api_version, sse_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]

_SOURCE_FILES = (
    "src/agent_loop.py",
    "src/agent_runs.py",
    "routes/chat_routes.py",
    "src/chat_outbox.py",
    # Forwarded verbatim onto the SSE stream by the four files above — see
    # the module docstring for exactly where and how each was found.
    "src/llm_core.py",
    "src/teacher_escalation.py",
)

#: `"type"` literals these files use for something OTHER than a top-level
#: SSE frame — provider message/content-block shapes, never sent as the
#: `type` of a `data: {...}` line the client's event switch reads.
_NOT_SSE_FRAME_TYPES = frozenset({
    "text",        # a text content block ({"type": "text", "text": ...})
    "thinking",    # a reasoning content block
    "image_url",   # an image-by-url content block
    "image",       # an image-by-source content block
    "base64",      # an image source's encoding
    "ephemeral",   # prompt-cache control ({"type": "ephemeral"})
    "tool_use",    # a native tool-call content block
    "tool_result", # a native tool-result content block
    "function",    # a tool/function definition or call block
    "refusal",     # a model-refusal content block
    "object",      # a JSON-schema {"type": "object", ...} (tool parameters)
    "url",         # a nested {"type": "url", ...} image/source shape
    # `response_format: {"type": "json_schema", ...}` in the request body
    # llm_core sends to an OpenAI-compatible engine -- constrained decoding,
    # nothing to do with the stream coming back (22-09-2026).
    "json_schema",
})

# `"type": "x"`, `'type': 'x'`, or the kwarg form `type="x"` — never
# `media_type="..."` (a StreamingResponse's MIME type, not an event type).
_TYPE_LITERAL_RE = re.compile(
    r"""(?:"type"\s*:\s*"(?P<dq>[A-Za-z0-9_]+)"|'type'\s*:\s*'(?P<sq>[A-Za-z0-9_]+)'|(?<!media_)\btype\s*=\s*"(?P<kw>[A-Za-z0-9_]+)")"""
)


def _emitted_types() -> set[str]:
    found: set[str] = set()
    for rel_path in _SOURCE_FILES:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for match in _TYPE_LITERAL_RE.finditer(text):
            literal = match.group("dq") or match.group("sq") or match.group("kw")
            found.add(literal)
    return found - _NOT_SSE_FRAME_TYPES


# ── (a) every type the source actually emits is catalogued ─────────────────

def test_every_emitted_sse_type_is_in_the_catalogue():
    emitted = _emitted_types()
    catalogued = sse_catalog.event_types()
    missing = sorted(emitted - catalogued)
    assert not missing, (
        "these SSE types are emitted by the source but missing from "
        f"docs/api/sse_events.json: {missing} — catalogue them (core or "
        "extended) before shipping, so a generated client does not see an "
        "event type it has never heard of"
    )


def test_the_exclusion_list_names_only_literals_actually_present():
    """A stale exclusion (the source stopped using a content-block type) is
    as dishonest as a missing catalogue entry — both let this guard claim
    more coverage than it has."""
    text = "\n".join(
        (REPO_ROOT / rel_path).read_text(encoding="utf-8") for rel_path in _SOURCE_FILES
    )
    for literal in _NOT_SSE_FRAME_TYPES:
        assert f'"{literal}"' in text or f"'{literal}'" in text, (
            f"{literal!r} is excluded as a non-event type literal but no "
            "longer appears in the scanned source — drop it from "
            "_NOT_SSE_FRAME_TYPES"
        )


# ── (b) every core event has non-empty fields, except *_resolved ───────────

def test_every_core_event_has_fields_unless_it_is_a_resolved_marker():
    catalog = sse_catalog.load_catalog()
    for event in catalog["events"]:
        if event.get("stability") != "core":
            continue
        name = event.get("type")
        if isinstance(name, str) and name.endswith("_resolved"):
            continue
        assert event.get("fields"), f"core event {name!r} has no fields"


# ── (c) schema_version tracks API_VERSION ───────────────────────────────────

def test_schema_version_matches_api_version():
    catalog = sse_catalog.load_catalog()
    assert catalog["schema_version"] == api_version.API_VERSION
    # And the module-level constant loaded at import time agrees — this is
    # what a hand-written `assert` in src/sse_catalog.py enforces at import,
    # exercised here as a normal (collectable) test rather than only an
    # import-time crash.
    assert sse_catalog.SCHEMA_VERSION == api_version.API_VERSION


# ── (d) the JSON is well-formed and has no duplicate types ─────────────────

def test_catalog_json_is_valid_and_has_no_duplicate_types():
    catalog = sse_catalog.load_catalog()
    assert isinstance(catalog["events"], list) and catalog["events"]
    types = [e["type"] for e in catalog["events"]]
    seen = [t for t in types if t is not None]
    assert len(seen) == len(set(seen)), "duplicate event type in the catalogue"
    # `null` (the plain-text delta frame) is the one type allowed to repeat
    # in principle — it never does, but the rule is only about strings.
    assert types.count(None) <= 1


# ── the guard actually fails on an uncatalogued type ────────────────────────

def test_the_guard_fails_closed_on_an_uncatalogued_type():
    """Not a test of the real catalogue — a test that the mechanism used by
    (a) above would have caught the exact class of bug this lot exists to
    prevent: a new `type` shipped with no catalogue entry."""
    catalog = sse_catalog.load_catalog()
    catalogued = sse_catalog.event_types(catalog)
    assert "a_type_nobody_catalogued" not in catalogued
