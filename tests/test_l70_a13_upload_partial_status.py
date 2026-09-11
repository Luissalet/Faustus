"""Lote 70a, punto A.13 — IDX-04's `status`/`partial`/`partial_reason`
(`src/upload_handler.py::save_upload`, via `document_processor.pdf_ingestion_signal`)
never reached the client: `routes/upload_routes.py`'s `/api/upload` and
`/api/upload/chunked/{id}/complete` responses built their own item dict and
dropped those three fields on the floor, even though `save_upload()` has
computed them since lote 66. Composer.tsx had nothing to warn a scanned or
cover-only-extracted PDF apart from a normally-read one.

Same harness idiom as `tests/test_upload_multifile.py`
(`_fake_handler`/`_request`/`_upload_endpoint`), reused rather than duplicated.
"""
from __future__ import annotations

import types

import routes.upload_routes as up
from tests.test_upload_multifile import _request, _upload_endpoint


def _fake_handler(extra: dict | None = None):
    h = types.SimpleNamespace()
    h.upload_rate_log = {}
    h.max_concurrent_uploads = 3

    def save_upload(u, client_ip, owner=None):
        h.upload_rate_log.setdefault(client_ip, []).append(0.0)
        meta = {
            "id": "0" * 32 + ".pdf", "name": getattr(u, "filename", "f"),
            "mime": "application/pdf", "size": 1, "hash": "h", "uploaded_at": "now",
            "width": None, "height": None, "is_duplicate": False,
        }
        if extra:
            meta.update(extra)
        return meta

    h.save_upload = save_upload
    return h


def _files(n=1):
    return [types.SimpleNamespace(filename=f"f{i}.pdf") for i in range(n)]


async def test_a_normally_read_pdf_reports_ready_not_partial():
    h = _fake_handler({"status": "ready", "partial": False, "partial_reason": None})
    endpoint = _upload_endpoint(h)
    result = await endpoint(_request(), _files())
    item = result["files"][0]
    assert item["status"] == "ready"
    assert item["partial"] is False
    assert item["partial_reason"] is None


async def test_a_scanned_pdf_reports_partial_with_its_reason():
    h = _fake_handler({"status": "partial", "partial": True, "partial_reason": "scanned"})
    endpoint = _upload_endpoint(h)
    result = await endpoint(_request(), _files())
    item = result["files"][0]
    assert item["status"] == "partial"
    assert item["partial"] is True
    assert item["partial_reason"] == "scanned"


async def test_a_cover_only_pdf_reports_its_own_reason():
    h = _fake_handler({"status": "partial", "partial": True, "partial_reason": "cover_only"})
    endpoint = _upload_endpoint(h)
    result = await endpoint(_request(), _files())
    assert result["files"][0]["partial_reason"] == "cover_only"


async def test_a_save_upload_that_predates_idx04_defaults_to_ready():
    """An older/stub save_upload that never learned these three keys must not
    make the route crash — and must not silently read as `partial`."""
    h = _fake_handler(extra=None)  # no status/partial/partial_reason keys at all
    endpoint = _upload_endpoint(h)
    result = await endpoint(_request(), _files())
    item = result["files"][0]
    assert item["status"] == "ready"
    assert item["partial"] is False
    assert item["partial_reason"] is None


def _chunked_complete_endpoint(router):
    for r in router.routes:
        if getattr(r, "path", None) == "/api/upload/chunked/{session_id}/complete":
            return r.endpoint
    raise AssertionError("chunked complete endpoint not found")


async def test_chunked_complete_also_forwards_the_three_fields(monkeypatch):
    h = _fake_handler({"status": "partial", "partial": True, "partial_reason": "scanned"})

    def complete_chunked_upload(session_id, client_ip, owner=None):
        return h.save_upload(types.SimpleNamespace(filename="scan.pdf"), client_ip, owner=owner)

    h.complete_chunked_upload = complete_chunked_upload
    router, _cleanup = up.setup_upload_routes(h)
    endpoint = _chunked_complete_endpoint(router)

    result = await endpoint(_request(), "sess-1")
    assert result["status"] == "partial"
    assert result["partial"] is True
    assert result["partial_reason"] == "scanned"
