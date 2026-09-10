"""Lote 64 — CTX-02 (pinned fragments + compaction log) and CTX-03 (exact
fragment behind a source_ref, search inside a big read).

Each test demonstrates the gap first (a comment marking what would fail
without the change) and then the closed behaviour.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.context_engine_routes import setup_context_engine_routes
from src.context_compactor import compact_with_integrity, _row_fingerprint
from src.context_engine import cache, candidates, compaction_pins, store
from src.context_engine.candidates import RetrievalRequest, ThreadedSource
from src.context_engine.contracts import ContextCandidate, ContextRequest
import src.read_plan as read_plan

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()

    @app.middleware("http")
    async def _as_luis(request, call_next):
        request.state.current_user = "luis"
        return await call_next(request)

    app.include_router(setup_context_engine_routes())
    try:
        yield TestClient(app)
    finally:
        store.use_path(None)
        cache.reset_working_set()


def _request_body(**over):
    body = {
        "request": {
            "actor": {"agent_id": "tester", "model": "test-model"},
            "execution": {"owner": "luis", "session_id": "s1", "project_id": "p1"},
            "task": {"intent": "chat", "phase": "act", "query": "hola"},
        },
    }
    body["request"].update(over)
    return body


# ── CTX-02: pinned fragments survive compact_with_integrity ────────────────

def _long_convo(n=10):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"turn {i} question about the plan"})
        msgs.append({"role": "assistant", "content": f"turn {i} answer, no code here"})
    return msgs


def test_pin_rescues_a_fragment_compaction_would_otherwise_fold(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        convo = _long_convo(10)
        # This is deep in "older" territory (well before the last keep_recent=4
        # turns) and carries no code block / ask_user answer — nothing else
        # protects it, so without a pin it lands inside the folded marker.
        target = convo[3]
        assert target["content"] == "turn 1 answer, no code here"
        fp = _row_fingerprint(target["role"], target["content"])

        # Without a pin: folded away (this is the pre-fix behaviour).
        out_unpinned, evidence = compact_with_integrity(
            list(convo), owner_id="luis", session_id="s-pin-1")
        assert evidence, "expected a fold to happen at all"
        assert not any(m is target or m.get("content") == target["content"]
                       for m in out_unpinned if m.get("role") == "user")

        # Pin it, then compact the SAME conversation again — session_id alone
        # is enough for compact_with_integrity to find the pin.
        compaction_pins.pin_fragment("luis", "s-pin-1", fp, excerpt=target["content"])
        out_pinned, evidence2 = compact_with_integrity(
            list(convo), owner_id="luis", session_id="s-pin-1")
        assert any(m.get("content") == target["content"] for m in out_pinned)
    finally:
        store.use_path(None)


def test_compaction_event_is_recorded_and_readable_back(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        assert compaction_pins.last_event("luis", "s-log-1") is None
        convo = _long_convo(10)
        _, evidence = compact_with_integrity(convo, owner_id="luis", session_id="s-log-1")
        assert evidence
        event = compaction_pins.last_event("luis", "s-log-1")
        assert event is not None
        assert event["kind"] == "integrity"
        assert event["folded_count"] > 0
        assert event["evidence_refs"]
        # Scoped: another owner or session sees nothing.
        assert compaction_pins.last_event("otro", "s-log-1") is None
        assert compaction_pins.last_event("luis", "s-log-other") is None
    finally:
        store.use_path(None)


def test_ctx02_pin_and_compaction_log_routes(client):
    body = {"session_id": "s-route-1", "role": "assistant",
            "content": "turn 1 answer, no code here", "excerpt": "keep this one"}
    r = client.post("/api/context/compaction/pins", json=body, headers=TOOL_HEADERS)
    assert r.status_code == 200, r.text
    pin = r.json()["pin"]
    assert pin["fingerprint"]

    r = client.get("/api/context/compaction/pins", params={"session_id": "s-route-1"})
    assert r.status_code == 200
    pins = r.json()["pins"]
    assert len(pins) == 1 and pins[0]["excerpt"] == "keep this one"

    # Now actually compact with that pin in place, through the real function.
    convo = _long_convo(10)
    compact_with_integrity(convo, owner_id="luis", session_id="s-route-1")

    r = client.get("/api/context/compaction/s-route-1")
    assert r.status_code == 200
    event = r.json()["event"]
    assert event is not None and event["pinned_skipped"] >= 1

    r = client.delete(f"/api/context/compaction/pins/{pin['fingerprint']}",
                      params={"session_id": "s-route-1"}, headers=TOOL_HEADERS)
    assert r.status_code == 200 and r.json()["removed"] is True
    r = client.get("/api/context/compaction/pins", params={"session_id": "s-route-1"})
    assert r.json()["pins"] == []


def test_ctx02_pin_route_requires_session_id(client):
    r = client.post("/api/context/compaction/pins",
                    json={"fingerprint": "abc"}, headers=TOOL_HEADERS)
    assert r.status_code == 400


# ── CTX-03: reopening the exact fragment behind a source_ref ───────────────

class _FakeSource(ThreadedSource):
    source_id = "l64_fake"
    sections = ("retrieved_memory",)
    handles = ("l64test:",)

    def __init__(self):
        self._store = {
            "l64test:1": ContextCandidate(
                candidate_id="c1", source_type="memory", source_ref="l64test:1",
                title="fact one", body="the exact original text of fact one",
                owner="luis"),
            "l64test:other-owner": ContextCandidate(
                candidate_id="c2", source_type="memory", source_ref="l64test:other-owner",
                title="not yours", body="should never come back to luis",
                owner="alguien-mas"),
        }

    def _fetch(self, source_ref, req):
        return self._store.get(source_ref)


@pytest.fixture()
def fake_source():
    candidates.reset_sources()
    candidates.register_source(_FakeSource())
    try:
        yield
    finally:
        candidates.reset_sources()


@pytest.mark.asyncio
async def test_fetch_ref_reopens_the_exact_fragment(fake_source):
    ctx_request = ContextRequest.parse({"execution": {"owner": "luis"}})
    retrieval = RetrievalRequest(request=ctx_request, explicit_refs=("l64test:1",))
    got = await candidates.fetch_ref("l64test:1", retrieval)
    assert got is not None
    assert got.body == "the exact original text of fact one"

    missing = await candidates.fetch_ref("l64test:does-not-exist", retrieval)
    assert missing is None


def test_ctx03_sources_fetch_route(client, fake_source):
    r = client.post("/api/context/sources/fetch",
                    json={**_request_body(), "source_ref": "l64test:1"},
                    headers=TOOL_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["retained"] is True
    assert data["candidate"]["body"] == "the exact original text of fact one"


def test_ctx03_sources_fetch_route_scopes_by_owner(client, fake_source):
    r = client.post("/api/context/sources/fetch",
                    json={**_request_body(), "source_ref": "l64test:other-owner"},
                    headers=TOOL_HEADERS)
    assert r.status_code == 200
    # Resolves, but belongs to somebody else: answered exactly like "gone".
    assert r.json()["retained"] is False


def test_ctx03_sources_fetch_route_missing_ref_is_clean_miss(client, fake_source):
    r = client.post("/api/context/sources/fetch",
                    json={**_request_body(), "source_ref": "l64test:nope"},
                    headers=TOOL_HEADERS)
    assert r.status_code == 200
    assert r.json()["retained"] is False


# ── CTX-03: search inside a big read's output ───────────────────────────────

def test_search_in_file_finds_a_rare_identifier(tmp_path):
    p = tmp_path / "big.py"
    lines = [f"line_{i} = {i}" for i in range(2000)]
    lines[1234] = "needle_unico_9f2a = 'find me'"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_plan.search_in_file(str(p), "needle_unico_9f2a")
    assert result["total_matches"] == 1
    assert result["shown"] == 1
    m = result["matches"][0]
    assert m["line"] == 1235  # 1-based
    assert "needle_unico_9f2a" in m["text"]


def test_search_in_file_context_lines_and_truncation(tmp_path):
    p = tmp_path / "many.py"
    lines = ["match" if i % 10 == 0 else f"noise_{i}" for i in range(500)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_plan.search_in_file(str(p), "match", max_matches=5, context_lines=1)
    assert result["shown"] == 5
    assert result["truncated"] is True
    assert result["total_matches"] == 50
    first = result["matches"][0]
    assert first["line"] == 1
    assert first["context_before"] == []  # first line has no lookback


def test_search_in_file_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_plan.search_in_file(str(tmp_path / "nope.py"), "x")


def test_ctx03_read_search_route(client, tmp_path):
    p = tmp_path / "target.py"
    p.write_text("a = 1\nneedle_special_xyz = 2\nb = 3\n", encoding="utf-8")
    r = client.post("/api/context/read/search",
                    json={"path": str(p), "query": "needle_special_xyz"},
                    headers=TOOL_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["shown"] == 1
    assert data["matches"][0]["line"] == 2


def test_ctx03_read_search_route_missing_file_is_404(client, tmp_path):
    r = client.post("/api/context/read/search",
                    json={"path": str(tmp_path / "nope.py"), "query": "x"},
                    headers=TOOL_HEADERS)
    assert r.status_code == 404
