"""Lote 69b — OBS-01: `GET /api/observability/trace/{call_id}` (lote 61's
route, `src/agent_runs.py::trace_for_call`) carries exactly the field names
`studio/src/adapters/observability.ts::callTraceFrom` parses — snake_case on
the wire, camelCase in the adapter (see studio/checks/l69b-obs01-trace.check.mjs
for the JS side of this same contract).

Mirrors tests/test_l61_obs01_trace_route.py's fixture style (a real FastAPI
app + TestClient against the actual router, COMUN.md rule 7), but asserts on
the field NAMES the Studio adapter depends on rather than the auth behaviour
already covered there.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.observability_routes as observability_routes
import src.agent_runs as agent_runs


def _client(monkeypatch, trace_result):
    monkeypatch.setattr(agent_runs, "trace_for_call", lambda call_id, *, session_id=None: trace_result)
    monkeypatch.setattr(observability_routes, "_verify_session_owner", lambda request, sid: None)
    app = FastAPI()
    app.include_router(observability_routes.setup_observability_routes())
    return TestClient(app)


def test_trace_carries_the_exact_field_names_the_studio_adapter_parses(monkeypatch):
    client = _client(monkeypatch, {
        "call_id": "call_1",
        "events": [
            {"type": "tool_start", "tool": "bash", "round": 2, "call_id": "call_1", "command": "ls"},
        ],
        "artifacts": [
            {
                "occurrence_id": "occ_1", "manifest_id": "man_1", "version": 3,
                "state": "final", "format": "png", "byte_size": 1024,
                "sha256": "abc123", "generator": "render", "created_at": "2026-01-01T00:00:00Z",
                "label": "cover.png", "kind": "image", "owner": "session_1",
            },
        ],
        "receipt": {"decision": "allowed", "call_id": "call_1"},
        "found": True,
    })
    body = client.get("/api/observability/trace/call_1", params={"session_id": "sid-a"}).json()

    assert body["call_id"] == "call_1"
    assert body["found"] is True

    event = body["events"][0]
    assert set(["type", "tool", "round", "call_id"]).issubset(event.keys())

    artifact = body["artifacts"][0]
    for key in ("occurrence_id", "manifest_id", "version", "state", "format",
                "byte_size", "sha256", "generator", "created_at", "label", "kind", "owner"):
        assert key in artifact, f"missing {key}"

    assert body["receipt"]["decision"] == "allowed"


def test_not_found_is_a_clean_shape_not_a_null_pile(monkeypatch):
    client = _client(monkeypatch, {
        "call_id": "nope", "events": [], "artifacts": [], "receipt": None, "found": False,
    })
    body = client.get("/api/observability/trace/nope", params={"session_id": "sid-a"}).json()
    assert body == {"call_id": "nope", "events": [], "artifacts": [], "receipt": None, "found": False}
