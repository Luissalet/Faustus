"""Lote 70a, punto A.15 (route half) — CTX-03's remaining piece:
`GET /api/context/packets/{packet_id}/items/{item_id}/fragment`.

`POST /sources/fetch` (lote 64) already resolves a raw `source_ref` through
`context_engine.candidates.fetch_ref`'s adapter registry; what was missing
is a way to ask for the fragment behind ONE manifest ROW (what
`Context.tsx::ManifestPane`'s "Ver fragmento" button actually has: an
item_id off a `GET .../manifest` row, not a source_ref it would have to
carry around separately). This route looks the row up in the same cached
manifest and delegates to the SAME `fetch_ref` — no second resolver.

Same harness as `tests/test_l64_ctx02_ctx03.py` (`client`/`_request_body`/
`_FakeSource`/`fake_source`), reused rather than duplicated.
"""
from __future__ import annotations

import pytest

from src.context_engine import cache
from src.context_engine.contracts import ContextRequest

from tests.test_l64_ctx02_ctx03 import (  # noqa: F401 - reused fixtures
    TOOL_HEADERS,
    _request_body,
    client,
    fake_source,
)


def _compile_and_inject(client, *, item_id="item1", source_ref="l64test:1",
                        session_id="s-frag-1"):
    """Compile a real packet (real ledger row, real owner) and then splice a
    synthetic manifest row referencing `source_ref` into the SAME cache
    entry `_remember_manifest` would have written — the compiler itself has
    no reason to pick up `_FakeSource`'s made-up prefix, so this is the
    direct way to get one onto a REAL packet's manifest without teaching the
    compiler about a test-only source."""
    body = _request_body(execution={"owner": "luis", "session_id": session_id})
    compiled = client.post("/api/context/compile", json=body,
                           headers=TOOL_HEADERS).json()
    packet_id = compiled["packet_id"]

    ctx_request = ContextRequest.parse(body["request"])
    scope = cache.scope_of(ctx_request)
    cache.working_set().put(
        scope, cache.PREFIX_PACKET + packet_id,
        {"owner": "luis", "manifest": [
            {"context_item_id": item_id, "section": "retrieved_memory",
             "source_type": "memory", "source_ref": source_ref},
        ], "summary": {}},
        cost_bytes=4096,
    )
    return packet_id


def test_a_manifest_items_fragment_is_reopened_live(client, fake_source):
    packet_id = _compile_and_inject(client)
    r = client.get(f"/api/context/packets/{packet_id}/items/item1/fragment")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolvable"] is True
    assert body["text"] == "the exact original text of fact one"


def test_an_unresolvable_source_ref_is_a_clean_miss_not_an_error(client, fake_source):
    packet_id = _compile_and_inject(client, source_ref="l64test:does-not-exist")
    r = client.get(f"/api/context/packets/{packet_id}/items/item1/fragment")
    assert r.status_code == 200
    body = r.json()
    assert body["resolvable"] is False
    assert body["text"] == ""


def test_a_fragment_belonging_to_another_owner_is_not_leaked(client, fake_source):
    packet_id = _compile_and_inject(client, source_ref="l64test:other-owner")
    r = client.get(f"/api/context/packets/{packet_id}/items/item1/fragment")
    assert r.status_code == 200
    assert r.json()["resolvable"] is False


def test_an_unknown_item_id_on_a_real_packet_is_404(client, fake_source):
    packet_id = _compile_and_inject(client)
    r = client.get(f"/api/context/packets/{packet_id}/items/does-not-exist/fragment")
    assert r.status_code == 404


def test_an_unknown_packet_id_is_404(client):
    r = client.get("/api/context/packets/does-not-exist/items/item1/fragment")
    assert r.status_code == 404


def test_another_owners_packet_is_404_not_leaked(client, fake_source):
    body = _request_body(execution={"owner": "luis", "session_id": "s-frag-owner"})
    compiled = client.post("/api/context/compile", json=body, headers=TOOL_HEADERS).json()
    packet_id = compiled["packet_id"]

    import routes.context_engine_routes as cer
    from starlette.testclient import TestClient
    from fastapi import FastAPI
    from core import middleware

    app = FastAPI()

    @app.middleware("http")
    async def _as_mallory(request, call_next):
        request.state.current_user = "mallory"
        return await call_next(request)

    app.include_router(cer.setup_context_engine_routes())
    other_client = TestClient(app)
    r = other_client.get(f"/api/context/packets/{packet_id}/items/item1/fragment")
    assert r.status_code == 404


def test_an_evicted_packets_manifest_is_an_honest_miss(client):
    """No `_compile_and_inject` splice here — the ledger row exists (from a
    real compile) but its manifest was never cached under a key this test
    controls, mirroring an eviction."""
    body = _request_body(execution={"owner": "luis", "session_id": "s-frag-evicted"})
    compiled = client.post("/api/context/compile", json=body, headers=TOOL_HEADERS).json()
    packet_id = compiled["packet_id"]
    cache.reset_working_set()  # the packet row survives this; its manifest cache does not

    r = client.get(f"/api/context/packets/{packet_id}/items/item1/fragment")
    assert r.status_code == 200
    body = r.json()
    assert body["resolvable"] is False
    assert "evicted" in body["note"]
