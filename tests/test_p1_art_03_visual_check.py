"""ART-03 — export and visual review: opening a PDF is not a visual pass.
`detect_page_overflow` reads real character/table bounding boxes against
each page's own dimensions; the acceptance criterion is that text pushed
past the page edge or a table cut off at the margin must NOT pass
automatically just because the file parses.
"""
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from src import document_actions as da

PAGE_W, PAGE_H = letter  # 612 x 792 pt


def _make_normal_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawString(72, 700, "This line comfortably fits within the page margins.")
    c.save()


def _make_overflow_text_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    # Starts well past the right margin — most of the string lands beyond x=612.
    c.drawString(580, 700, "This text runs off the right edge of the page")
    c.save()


def _make_table_cut_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    x0, x1, x2 = 400, 550, 700   # x2 > PAGE_W (612) — the table's right edge is cut off
    y0, y1, y2 = 600, 650, 700
    for x in (x0, x1, x2):
        c.line(x, y0, x, y2)
    for y in (y0, y1, y2):
        c.line(x0, y, x2, y)
    c.save()


def test_normal_pdf_passes_visual_check(tmp_path):
    pytest.importorskip("pdfplumber")  # optional dependency (requirements-optional.txt)
    path = tmp_path / "normal.pdf"
    _make_normal_pdf(path)
    report = da.detect_page_overflow(str(path))
    assert report["pages"] == 1
    assert report["overflow_pages"] == []
    assert report["table_cut_pages"] == []
    assert report["passes_visual_check"] is True


def test_text_past_the_margin_fails_visual_check(tmp_path):
    pytest.importorskip("pdfplumber")  # optional dependency (requirements-optional.txt)
    path = tmp_path / "overflow.pdf"
    _make_overflow_text_pdf(path)
    report = da.detect_page_overflow(str(path))
    assert report["passes_visual_check"] is False
    assert len(report["overflow_pages"]) == 1
    hit = report["overflow_pages"][0]
    assert hit["page"] == 1
    assert hit["reason"] == "text_outside_page"
    assert hit["detail"]["overflow_pt"] > 0


def test_table_cut_off_at_the_edge_fails_visual_check(tmp_path):
    pytest.importorskip("pdfplumber")  # optional dependency (requirements-optional.txt)
    path = tmp_path / "table_cut.pdf"
    _make_table_cut_pdf(path)
    report = da.detect_page_overflow(str(path))
    assert report["passes_visual_check"] is False
    assert len(report["table_cut_pages"]) == 1
    hit = report["table_cut_pages"][0]
    assert hit["reason"] == "table_cut"
    assert hit["overflow_pt"] > 0


def test_missing_pdfplumber_reports_a_declared_error_not_a_silent_pass(tmp_path, monkeypatch):
    path = tmp_path / "normal.pdf"
    _make_normal_pdf(path)
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(da.DocumentActionError):
        da.detect_page_overflow(str(path))


# ---- route wiring: GET /api/document/{doc_id}/visual-check ----------------

def test_visual_check_route_flags_overflow_pdf(tmp_path, monkeypatch):
    pytest.importorskip("pdfplumber")  # optional dependency (requirements-optional.txt)
    from unittest.mock import MagicMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from routes.document import document_routes as routes

    pdf_path = tmp_path / "overflow.pdf"
    _make_overflow_text_pdf(pdf_path)

    engine = create_engine('sqlite:///' + str(tmp_path / 'docs.db'),
                           connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', sessions)
    monkeypatch.setattr(routes, 'get_current_user', lambda request: 'alice')
    monkeypatch.setattr(routes, '_verify_doc_owner', lambda session, doc, owner: None)
    monkeypatch.setattr(routes, '_resolve_user_upload_path',
                        lambda upload_handler, upload_id, user, auth_manager: str(pdf_path))
    with sessions() as session:
        import uuid as _uuid
        upload_id = _uuid.uuid4().hex
        session.add(db.Document(
            id='doc1', title='Scan', language='markdown',
            current_content=f'<!-- pdf_source upload_id="{upload_id}" -->\n# Scan\n',
            version_count=1, owner='alice', is_active=True))
        session.commit()

    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock(), upload_handler=MagicMock()))
    with TestClient(app) as client:
        resp = client.get('/api/document/doc1/visual-check')
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['passes_visual_check'] is False
        assert body['overflow_pages'][0]['reason'] == 'text_outside_page'

        missing = client.get('/api/document/does-not-exist/visual-check')
        assert missing.status_code == 404
    engine.dispose()
