"""OBS-03 (lote 42): every HTTP error response carries `error_class`.

MAPA_REUTILIZACION.md's OBS-03 row: "Los errores de rutas HTTP antiguas aun
no llevan error_class" -- only llm_core.py's own HTTPExceptions were
annotated (`exc.error_class = ...`), which never reaches the JSON body a
plain route's `raise HTTPException(...)` produces. `SecurityHeadersMiddleware`
(core/middleware.py) is already mounted on every response app.py serves
(`app.add_middleware(SecurityHeadersMiddleware)`), so this wires the fix
there instead of adding a second mount point in app.py (not owned by this
lote).

Uses a real ASGI app + TestClient (rule 7: this crosses an HTTP boundary),
built from just the pieces under test -- not the full app.py, which needs a
live DB/session manager this middleware has nothing to do with.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/boom-404")
    async def boom_404():
        raise HTTPException(404, "no such thing")

    @app.get("/boom-409")
    async def boom_409():
        raise HTTPException(409, "base revision mismatch")

    @app.get("/boom-legacy-shape")
    async def boom_legacy_shape():
        # Mirrors app.py's four hand-built exception handlers: {"error", "message"},
        # no "detail" key at all.
        return JSONResponse(status_code=502, content={"error": "LLM_SERVICE_ERROR", "message": "down"})

    @app.get("/boom-already-tagged")
    async def boom_already_tagged():
        return JSONResponse(status_code=500, content={"detail": "oops", "error_class": "unknown.panic"})

    @app.get("/ok")
    async def ok():
        return {"hello": "world"}

    @app.get("/stream")
    async def stream():
        async def gen():
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return TestClient(app)


def test_plain_http_exception_gets_an_error_class(client):
    r = client.get("/boom-404")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "no such thing"  # untouched
    assert body["error_class"] == "resource.not_found"


def test_409_maps_to_conflict_not_the_generic_schema_bucket(client):
    r = client.get("/boom-409")
    assert r.status_code == 409
    assert r.json()["error_class"] == "conflict.concurrent_write"


def test_legacy_error_message_shape_is_extended_not_replaced(client):
    """app.py's own four exception handlers use {"error", "message"}, not
    "detail" -- both fields must survive untouched."""
    r = client.get("/boom-legacy-shape")
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "LLM_SERVICE_ERROR"
    assert body["message"] == "down"
    assert body["error_class"] == "transport.llm_service_error"


def test_existing_error_class_is_never_overwritten(client):
    r = client.get("/boom-already-tagged")
    assert r.json()["error_class"] == "unknown.panic"


def test_success_response_is_not_touched(client):
    r = client.get("/ok")
    assert r.status_code == 200
    assert r.json() == {"hello": "world"}
    assert "error_class" not in r.json()


def test_streaming_response_body_is_never_buffered(client):
    """A 200 SSE stream (chat) must pass through byte-for-byte -- this is the
    regression this lote's design has to avoid above all: buffering
    `response.body_iterator` for something that is not a small JSON error
    body would break chat streaming."""
    with client.stream("GET", "/stream") as r:
        assert r.status_code == 200
        chunks = list(r.iter_bytes())
    assert b"".join(chunks) == b"data: one\n\ndata: two\n\n"


def test_content_length_header_matches_the_extended_body(client):
    r = client.get("/boom-404")
    assert int(r.headers["content-length"]) == len(r.content)
