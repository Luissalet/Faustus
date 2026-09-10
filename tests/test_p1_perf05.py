"""PERF-05 - transferencias y adjuntos grandes: chunked, resumable uploads.

Adds `start_chunked_upload` / `write_chunk` / `chunked_upload_status` /
`complete_chunked_upload` / `cancel_chunked_upload` to src/upload_handler.py
(class UploadHandler) and five routes under routes/upload_routes.py
(`/api/upload/chunked/...`). `complete_chunked_upload` hands the assembled
file to the SAME `save_upload()` every other upload goes through (dedup,
index, thumbnails) -- not a second store.

Driven through the real router with TestClient (COMUN rule 7) against a real
UploadHandler backed by tmp_path -- no mock standing in for the upload
pipeline itself.
"""
from __future__ import annotations

import hashlib
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.upload_handler import UploadHandler
from routes.upload_routes import setup_upload_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    handler = UploadHandler(str(tmp_path), str(upload_dir))
    router, _cleanup = setup_upload_routes(handler)
    app = FastAPI()
    app.include_router(router)  # router already carries prefix="/api/upload"
    return TestClient(app), handler


def _start(client, *, filename="movie.mp4", total_size, chunk_size=None, expected_sha256=""):
    body = {"filename": filename, "total_size": total_size}
    if chunk_size:
        body["chunk_size"] = chunk_size
    if expected_sha256:
        body["expected_sha256"] = expected_sha256
    r = client.post("/api/upload/chunked/start", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── start / status / chunk / complete happy path ───────────────────────────


def test_full_chunked_upload_round_trip(client):
    c, handler = client
    payload = os.urandom(50_000)
    expected = hashlib.sha256(payload).hexdigest()
    session = _start(c, total_size=len(payload), chunk_size=20_000, expected_sha256=expected)
    assert session["total_chunks"] == 3
    assert session["received_chunks"] == []

    chunks = [payload[i:i + 20_000] for i in range(0, len(payload), 20_000)]
    for i, chunk in enumerate(chunks):
        r = c.put(f"/api/upload/chunked/{session['session_id']}/chunk/{i}", content=chunk)
        assert r.status_code == 200, r.text
        assert i in r.json()["received_chunks"]

    status = c.get(f"/api/upload/chunked/{session['session_id']}/status").json()
    assert status["received_chunks"] == [0, 1, 2]

    r = c.post(f"/api/upload/chunked/{session['session_id']}/complete")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["checksum_sha256"] == expected
    assert meta["size"] == len(payload)

    # save_upload's real pipeline ran: the file is really on disk, findable
    # by the id this route returned.
    info = handler.get_upload_info(meta["id"])
    assert info is not None
    with open(info["path"], "rb") as f:
        assert f.read() == payload

    # The session directory is gone once complete: no leftover parts.
    assert not os.path.isdir(os.path.join(str(handler.upload_dir), ".chunked", session["session_id"]))


def test_chunks_can_arrive_out_of_order_and_be_resumed_after(client):
    c, handler = client
    payload = os.urandom(30_000)
    session = _start(c, total_size=len(payload), chunk_size=10_000)
    sid = session["session_id"]
    chunks = [payload[i:i + 10_000] for i in range(0, len(payload), 10_000)]

    # Send chunk 2, then 0 -- simulate a client that resumes out of order.
    c.put(f"/api/upload/chunked/{sid}/chunk/2", content=chunks[2])
    status = c.get(f"/api/upload/chunked/{sid}/status").json()
    assert status["received_chunks"] == [2]   # resumability: exactly what's on disk

    c.put(f"/api/upload/chunked/{sid}/chunk/0", content=chunks[0])
    # Not complete yet -- chunk 1 still missing.
    r = c.post(f"/api/upload/chunked/{sid}/complete")
    assert r.status_code == 409
    assert "missing chunk" in r.json()["detail"]

    c.put(f"/api/upload/chunked/{sid}/chunk/1", content=chunks[1])
    r = c.post(f"/api/upload/chunked/{sid}/complete")
    assert r.status_code == 200
    with open(handler.get_upload_info(r.json()["id"])["path"], "rb") as f:
        assert f.read() == payload   # reassembled in INDEX order, not arrival order


# ── hash verification ───────────────────────────────────────────────────


def test_a_wrong_declared_hash_is_refused_and_cleans_up(client):
    c, handler = client
    payload = os.urandom(5_000)
    session = _start(c, total_size=len(payload), chunk_size=5_000,
                     expected_sha256="0" * 64)  # deliberately wrong
    sid = session["session_id"]
    c.put(f"/api/upload/chunked/{sid}/chunk/0", content=payload)
    r = c.post(f"/api/upload/chunked/{sid}/complete")
    assert r.status_code == 409
    assert "hash mismatch" in r.json()["detail"]
    assert not os.path.isdir(os.path.join(str(handler.upload_dir), ".chunked", sid))


# ── size/shape validation ───────────────────────────────────────────────


def test_a_chunk_of_the_wrong_size_is_refused(client):
    c, _ = client
    session = _start(c, total_size=100, chunk_size=50)
    r = c.put(f"/api/upload/chunked/{session['session_id']}/chunk/0", content=b"x" * 10)
    assert r.status_code == 400
    assert "expected 50" in r.json()["detail"]


def test_an_out_of_range_index_is_refused(client):
    c, _ = client
    session = _start(c, total_size=100, chunk_size=50)  # 2 chunks: 0, 1
    r = c.put(f"/api/upload/chunked/{session['session_id']}/chunk/5", content=b"x" * 50)
    assert r.status_code == 400


def test_start_refuses_a_total_size_over_the_chunked_cap(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("src.upload_handler.CHUNKED_UPLOAD_MAX_BYTES", 1000)
    r = c.post("/api/upload/chunked/start", json={"filename": "big.bin", "total_size": 5000})
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_removes_the_session_and_future_writes_404(client):
    c, handler = client
    session = _start(c, total_size=10, chunk_size=10)
    sid = session["session_id"]
    r = c.delete(f"/api/upload/chunked/{sid}")
    assert r.status_code == 200
    assert not os.path.isdir(os.path.join(str(handler.upload_dir), ".chunked", sid))
    r = c.get(f"/api/upload/chunked/{sid}/status")
    assert r.status_code == 404
    r = c.delete(f"/api/upload/chunked/{sid}")
    assert r.status_code == 404


def test_an_invalid_session_id_never_touches_the_filesystem_outside_chunked_dir(client):
    c, _ = client
    r = c.get("/api/upload/chunked/../../../etc/status")
    assert r.status_code in (404, 400)
    r = c.get("/api/upload/chunked/not-a-valid-session-id/status")
    assert r.status_code == 400


# ── save_upload's own size cap is unaffected for ordinary (non-chunked) callers ──


def test_plain_save_upload_still_uses_the_chat_cap_by_default(client, monkeypatch):
    """max_size_override defaults to None: nothing about a normal (single
    POST) upload's size limit changed."""
    c, handler = client
    monkeypatch.setattr(handler, "max_upload_size", 10)
    from types import SimpleNamespace
    import io

    class _FakeUpload:
        def __init__(self, data):
            self.file = io.BytesIO(data)
            self.filename = "small.txt"

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        handler.save_upload(_FakeUpload(b"x" * 100), "127.0.0.1")
    assert exc.value.status_code == 400
    assert "10 bytes" in exc.value.detail


def test_chunked_complete_uses_the_larger_chunked_cap_not_the_chat_cap(client, monkeypatch):
    """The real bug this override exists to avoid: reusing save_upload()
    verbatim for the assembled file would reject anything chunked-uploaded
    above the 10 MB chat cap, defeating the point of a large-file path."""
    c, handler = client
    monkeypatch.setattr(handler, "max_upload_size", 10)  # far below the payload below
    payload = os.urandom(2_000)
    session = _start(c, total_size=len(payload), chunk_size=2_000)
    c.put(f"/api/upload/chunked/{session['session_id']}/chunk/0", content=payload)
    r = c.post(f"/api/upload/chunked/{session['session_id']}/complete")
    assert r.status_code == 200, r.text
