"""S1.2: the versioned catalogue of SSE event types `docs/api/sse_events.json`
declares — the SDK's contract for what can arrive on `POST /api/chat_stream`
and `GET /api/chat/resume/{session_id}`.

The file is data, not code, on purpose: a TypeScript client generates its
`core` event union from it without parsing Python. This module is the one
place the SERVER reads that same file back, so the guard in
`tests/test_sse_catalog.py` can fail the build the moment a source file
starts emitting a `type` the catalogue does not know about, instead of that
gap being discovered by an external client seeing an event it cannot parse.

`SCHEMA_VERSION` is asserted equal to `src.api_version.API_VERSION` at import
time: the catalogue's `schema_version` field IS the `schema_version` stamped
on every SSE envelope (`src/agent_runs.py::_observability_fields`), so the
two drifting apart would mean the document lies about the wire it describes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set

from src import api_version

#: docs/api/sse_events.json, next to this module's repo root — never data/,
#: never mutated at runtime.
CATALOG_PATH = Path(__file__).resolve().parents[1] / "docs" / "api" / "sse_events.json"


def load_catalog(path: Path | str | None = None) -> Dict[str, Any]:
    """Parse and return the catalogue document as-is (dict, not a model —
    every consumer so far only needs `events`/`schema_version`/`framing`)."""
    catalog_path = Path(path) if path is not None else CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _types(catalog: Dict[str, Any], *, stability: str | None = None) -> Set[str]:
    """Every non-null `type` in `catalog["events"]`, optionally filtered to
    one `stability` bucket. `null` (the plain-text `delta` frame, which
    carries no `type` key at all) is deliberately excluded — it is not a
    string a guard can compare against an emitted literal."""
    out: Set[str] = set()
    for event in catalog.get("events", ()):
        if not isinstance(event, dict):
            continue
        if stability is not None and event.get("stability") != stability:
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            out.add(event_type)
    return out


def event_types(catalog: Dict[str, Any] | None = None) -> FrozenSet[str]:
    """Every catalogued type, core and extended together."""
    return frozenset(_types(catalog if catalog is not None else load_catalog()))


def core_event_types(catalog: Dict[str, Any] | None = None) -> FrozenSet[str]:
    """Only the `stability: "core"` types — the ones a generated client
    types explicitly rather than folding into `UnknownEvent`."""
    return frozenset(_types(catalog if catalog is not None else load_catalog(), stability="core"))


#: Loaded once at import so a caller doing `from src.sse_catalog import
#: SCHEMA_VERSION` gets the real value without re-reading the file.
_CATALOG = load_catalog()
SCHEMA_VERSION: str = str(_CATALOG.get("schema_version") or "")

assert SCHEMA_VERSION == api_version.API_VERSION, (
    f"docs/api/sse_events.json schema_version={SCHEMA_VERSION!r} has drifted from "
    f"src.api_version.API_VERSION={api_version.API_VERSION!r} — every SSE envelope "
    "stamps API_VERSION as its own schema_version (src/agent_runs.py's "
    "_observability_fields), so the catalogue would be describing a wire "
    "shape the server no longer sends. Bump whichever one is stale."
)
