"""GET /api/research/export/{id} — the download route for a finished report.

A research report can contain anything the user asked about, so the first
thing these tests pin is the ownership gate: someone else's report and a
report that does not exist must be indistinguishable (404 both, never 403).

The rest is what makes a download usable rather than merely successful — a
Content-Disposition both halves of the world can read, a 400 that names the
formats that do work, and a missing optional package surfacing as a 415
carrying the message that names the package to install, instead of a 500.
"""

import asyncio
import builtins
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routes.research_routes import setup_research_routes

SPANISH_QUERY = "¿Es eficaz la fisioterapia para el dolor lumbar crónico?"


@pytest.fixture(autouse=True)
def _redirect_research_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "routes.research_routes.DEEP_RESEARCH_DIR",
        str(tmp_path / "data" / "deep_research"),
    )


def _request(user: str):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _route(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _research_handler():
    handler = MagicMock()
    handler._active_tasks = {}
    return handler


def _write_research(data_dir, session_id: str, **overrides):
    data = {
        "query": SPANISH_QUERY,
        "status": "done",
        "result": "Resultado",
        "raw_report": "## Resumen\n\nEl ejercicio terapéutico es eficaz.\n",
        "sources": [{"title": "Cochrane", "url": "https://cochrane.org/lbp"}],
        "raw_findings": [],
        "stats": {"Duration": "182.4s", "Rounds": 3, "Model": "qwen3:14b"},
        "category": "health",
        "started_at": 1772000000.0,
        "completed_at": 1772000182.4,
        "owner": "alice",
    }
    data.update(overrides)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{session_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _export(tmp_path, session_id="alice-report", user="alice", fmt="md",
            write=True, **overrides):
    data_dir = tmp_path / "data" / "deep_research"
    if write:
        _write_research(data_dir, "alice-report", **overrides)
    target = _route(setup_research_routes(_research_handler()),
                    "/api/research/export/{session_id}", "GET")
    return asyncio.run(target(session_id=session_id, request=_request(user),
                              format=fmt))


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session_id,owner", [
    ("alice-report", "bob"),        # someone else's report
    ("alice-report", None),         # a legacy report with no owner stamped
])
def test_export_404s_for_a_report_the_caller_does_not_own(tmp_path, session_id, owner):
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, session_id=session_id, owner=owner)
    assert exc.value.status_code == 404


def test_export_404s_for_a_report_that_does_not_exist(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, session_id="never-existed")
    assert exc.value.status_code == 404


def test_a_foreign_report_is_indistinguishable_from_a_missing_one(tmp_path):
    """404, and the same 404 — the status must not leak that the report exists."""
    with pytest.raises(HTTPException) as foreign:
        _export(tmp_path, owner="bob")
    with pytest.raises(HTTPException) as missing:
        _export(tmp_path, session_id="never-existed")
    assert foreign.value.status_code == missing.value.status_code == 404
    assert foreign.value.detail == missing.value.detail


def test_export_rejects_a_malformed_session_id(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, session_id="../../etc/passwd")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_markdown_export_returns_the_document_with_download_headers(tmp_path):
    response = _export(tmp_path, fmt="md")
    assert response.status_code == 200
    assert response.media_type == "text/markdown; charset=utf-8"

    body = response.body.decode("utf-8")
    assert body.startswith("# " + SPANISH_QUERY)
    assert "El ejercicio terapéutico es eficaz." in body
    assert "Faustus" in body

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    assert 'filename="research_Es_eficaz' in disposition
    assert "filename*=UTF-8''research_Es_eficaz" in disposition
    assert disposition.endswith("_20260225_061622.md")


@pytest.mark.parametrize("fmt,media_type", [
    ("txt", "text/plain; charset=utf-8"),
    ("html", "text/html; charset=utf-8"),
    ("json", "application/json"),
])
def test_text_formats_come_back_with_their_media_type(tmp_path, fmt, media_type):
    response = _export(tmp_path, fmt=fmt)
    assert response.status_code == 200
    assert response.media_type == media_type
    assert response.body
    assert response.headers["content-disposition"].endswith(".%s" % fmt)


def test_docx_export_returns_a_word_document(tmp_path):
    pytest.importorskip("docx")
    response = _export(tmp_path, fmt="docx")
    assert response.status_code == 200
    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert response.body[:2] == b"PK"


def test_pdf_export_returns_a_pdf(tmp_path):
    pytest.importorskip("reportlab")
    response = _export(tmp_path, fmt="pdf")
    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.body[:5] == b"%PDF-"


# ---------------------------------------------------------------------------
# over real HTTP
#
# Calling the endpoint function directly (as every other research route test
# does) skips FastAPI: query defaults arrive as Query objects and an
# HTTPException never becomes a status code. These few go through the stack so
# the parts only it can get wrong — the ?format= default, the 415, and a
# Content-Disposition header Starlette has to be able to encode — are covered.
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(setup_research_routes(_research_handler()))

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    _write_research(tmp_path / "data" / "deep_research", "alice-report")
    return TestClient(app, raise_server_exceptions=False)


ALICE = {"x-user": "alice"}


def test_http_format_defaults_to_markdown(client):
    response = client.get("/api/research/export/alice-report", headers=ALICE)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text.startswith("# " + SPANISH_QUERY)


def test_http_disposition_carries_both_filename_forms(client):
    response = client.get("/api/research/export/alice-report?format=md", headers=ALICE)
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="research_Es_eficaz')
    assert "filename*=UTF-8''" in disposition
    assert disposition.isascii()      # Starlette rejects a non-Latin-1 header


def test_http_unknown_format_is_400(client):
    response = client.get("/api/research/export/alice-report?format=rtf", headers=ALICE)
    assert response.status_code == 400
    assert "md" in response.json()["detail"]


def test_http_foreign_report_is_404(client):
    response = client.get("/api/research/export/alice-report", headers={"x-user": "bob"})
    assert response.status_code == 404


def test_http_missing_renderer_is_415(client, monkeypatch):
    import src.chat_export_docx as docx_renderer

    monkeypatch.setattr(docx_renderer, "_DX", None)
    real_import = builtins.__import__

    def refuse_docx(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("No module named 'docx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_docx)

    response = client.get("/api/research/export/alice-report?format=docx", headers=ALICE)
    assert response.status_code == 415
    assert response.json()["detail"] == docx_renderer.DOCX_MISSING


def test_http_export_formats_probe(client):
    response = client.get("/api/research/export-formats", headers=ALICE)
    assert response.status_code == 200
    assert response.json()["formats"]["md"] is True


# ---------------------------------------------------------------------------
# bad input, missing renderer
# ---------------------------------------------------------------------------

def test_unknown_format_is_a_400_naming_the_supported_ones(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, fmt="rtf")
    assert exc.value.status_code == 400
    for fmt in ("md", "docx", "pdf", "html", "txt", "json"):
        assert fmt in exc.value.detail


def test_the_format_check_happens_after_the_ownership_check(tmp_path):
    """A bad format on someone else's report is still a 404, not a 400.

    A 400 here would confirm the report exists to anyone who asks with a
    deliberately wrong format.
    """
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, owner="bob", fmt="rtf")
    assert exc.value.status_code == 404


def test_missing_renderer_is_a_415_with_the_message_verbatim(tmp_path, monkeypatch):
    """python-docx not installed: 415 carrying the text that names the package."""
    import src.chat_export_docx as docx_renderer

    monkeypatch.setattr(docx_renderer, "_DX", None)
    real_import = builtins.__import__

    def refuse_docx(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("No module named 'docx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_docx)

    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, fmt="docx")

    assert exc.value.status_code == 415
    assert exc.value.detail == docx_renderer.DOCX_MISSING
    assert "python-docx" in exc.value.detail


def test_a_corrupt_research_file_404s_rather_than_leaking_a_traceback(tmp_path):
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "alice-report.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        _export(tmp_path, write=False)
    # Unreadable JSON can't be owner-checked either, so the gate fires first.
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# the availability probe
# ---------------------------------------------------------------------------

def test_export_formats_reports_every_format(tmp_path):
    target = _route(setup_research_routes(_research_handler()),
                    "/api/research/export-formats", "GET")
    out = asyncio.run(target(request=_request("alice")))
    assert set(out["formats"]) == {"md", "docx", "pdf", "html", "txt", "json"}
    assert out["formats"]["md"] is True
    assert isinstance(out["formats"]["docx"], bool)


def test_export_formats_requires_a_user(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.research_routes._auth_disabled", lambda: False)
    target = _route(setup_research_routes(_research_handler()),
                    "/api/research/export-formats", "GET")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(request=_request(None)))
    assert exc.value.status_code == 401
