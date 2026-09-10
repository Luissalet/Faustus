"""ART-07 — PDFs and complex files: split/merge with declared page/size
budgets that refuse BEFORE producing an oversized result, rather than
silently writing something the rest of the app can't open or serve.
"""
import io
import os
import zipfile

import pytest
from reportlab.pdfgen import canvas

from src import document_actions as da


def _make_pdf(path, n_pages):
    c = canvas.Canvas(str(path))
    for i in range(n_pages):
        c.drawString(72, 700, f"page {i + 1}")
        c.showPage()
    c.save()


# ---- split_pdf --------------------------------------------------------

def test_split_pdf_produces_one_file_per_page_by_default(tmp_path):
    src = tmp_path / "doc.pdf"
    _make_pdf(src, 5)
    out_dir = tmp_path / "out"
    report = da.split_pdf(str(src), str(out_dir))
    assert report["input_pages"] == 5
    assert len(report["outputs"]) == 5
    for entry in report["outputs"]:
        assert os.path.isfile(entry["path"])


def test_split_pdf_groups_pages_per_file(tmp_path):
    src = tmp_path / "doc.pdf"
    _make_pdf(src, 5)
    report = da.split_pdf(str(src), str(tmp_path / "out"), pages_per_file=2)
    assert len(report["outputs"]) == 3  # 2 + 2 + 1
    assert report["outputs"][0] == {
        "path": report["outputs"][0]["path"], "first_page": 1, "last_page": 2}
    assert report["outputs"][-1]["first_page"] == 5 and report["outputs"][-1]["last_page"] == 5


def test_split_pdf_refuses_over_budget_and_writes_nothing(tmp_path):
    src = tmp_path / "doc.pdf"
    _make_pdf(src, 10)
    out_dir = tmp_path / "out"
    with pytest.raises(da.DocumentActionError):
        da.split_pdf(str(src), str(out_dir), pages_per_file=1, max_files=3)
    assert not out_dir.exists()  # nothing was written before the refusal


# ---- merge_pdfs ---------------------------------------------------------

def test_merge_pdfs_concatenates_in_order(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _make_pdf(a, 2)
    _make_pdf(b, 3)
    out = tmp_path / "merged.pdf"
    report = da.merge_pdfs([str(a), str(b)], str(out))
    assert report["pages"] == 5
    assert out.exists()
    from pypdf import PdfReader
    assert len(PdfReader(str(out)).pages) == 5


def test_merge_pdfs_refuses_over_page_budget_and_writes_nothing(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _make_pdf(a, 3)
    _make_pdf(b, 3)
    out = tmp_path / "merged.pdf"
    with pytest.raises(da.DocumentActionError):
        da.merge_pdfs([str(a), str(b)], str(out), max_total_pages=4)
    assert not out.exists()


def test_merge_pdfs_refuses_over_byte_budget_before_opening_anything(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _make_pdf(a, 1)
    _make_pdf(b, 1)
    out = tmp_path / "merged.pdf"
    tiny_budget = os.path.getsize(a) + 1  # smaller than both files combined
    with pytest.raises(da.DocumentActionError):
        da.merge_pdfs([str(a), str(b)], str(out), max_total_bytes=tiny_budget)
    assert not out.exists()


# ---- route wiring -----------------------------------------------------

def _app_with_document_routes(monkeypatch=None):
    from unittest.mock import MagicMock
    from fastapi import FastAPI
    from routes.document import document_routes as routes
    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock(), upload_handler=MagicMock()))
    return app, routes


def test_pdf_split_route_returns_a_zip_of_parts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app, routes = _app_with_document_routes()
    monkeypatch.setattr(routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)

    src = tmp_path / "doc.pdf"
    _make_pdf(src, 3)
    with TestClient(app) as client:
        with open(src, "rb") as fh:
            resp = client.post(
                "/api/documents/pdf/split",
                files={"file": ("doc.pdf", fh, "application/pdf")},
                data={"pages_per_file": "1"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-input-pages"] == "3"
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert len(zf.namelist()) == 3


def test_pdf_merge_route_returns_one_pdf(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app, routes = _app_with_document_routes()
    monkeypatch.setattr(routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)

    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _make_pdf(a, 2)
    _make_pdf(b, 1)
    with TestClient(app) as client:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            resp = client.post(
                "/api/documents/pdf/merge",
                files=[("files", ("a.pdf", fa, "application/pdf")),
                      ("files", ("b.pdf", fb, "application/pdf"))],
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-output-pages"] == "3"
        from pypdf import PdfReader
        assert len(PdfReader(io.BytesIO(resp.content)).pages) == 3


def test_pdf_merge_route_rejects_a_single_file(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app, routes = _app_with_document_routes()
    monkeypatch.setattr(routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)

    a = tmp_path / "a.pdf"
    _make_pdf(a, 1)
    with TestClient(app) as client:
        with open(a, "rb") as fa:
            resp = client.post("/api/documents/pdf/merge",
                               files=[("files", ("a.pdf", fa, "application/pdf"))])
        assert resp.status_code == 400
