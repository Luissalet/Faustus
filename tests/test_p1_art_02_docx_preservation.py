"""ART-02 — real document editing: before converting a DOCX into the
markdown-backed editor, `analyze_docx_preservation` says exactly what a
round-trip would drop (comments, tracked changes, numbering, images) so the
caller can warn and let the user cancel, instead of silently losing them on
the first save.
"""
import pytest
import docx

from src import document_actions as da


def _make_plain_docx(path):
    document = docx.Document()
    document.add_paragraph("Just a plain paragraph, nothing fancy.")
    document.save(path)


def _make_docx_with_image(path, png_path):
    document = docx.Document()
    document.add_paragraph("Has a picture below.")
    document.add_picture(str(png_path))
    document.save(path)


def _make_docx_with_numbering(path):
    document = docx.Document()
    document.add_paragraph("Item one", style="List Number")
    document.add_paragraph("Item two", style="List Number")
    document.save(path)


def _make_docx_with_comment(path):
    document = docx.Document()
    p = document.add_paragraph("Please review this line.")
    document.add_comment(p.runs[0], text="Looks good?", author="Reviewer")
    document.save(path)


@pytest.fixture()
def tiny_png(tmp_path):
    # A minimal 1x1 PNG, valid enough for python-docx to embed.
    from PIL import Image
    path = tmp_path / "dot.png"
    Image.new("RGB", (2, 2), color="red").save(path)
    return path


def test_plain_docx_is_safe_to_convert_silently(tmp_path):
    path = tmp_path / "plain.docx"
    _make_plain_docx(path)
    report = da.analyze_docx_preservation(str(path))
    assert report["safe_to_convert_silently"] is True
    assert report["not_preservable_on_markdown_conversion"] == []
    assert report["image_count"] == 0
    assert report["has_comments"] is False


def test_docx_with_image_is_flagged(tmp_path, tiny_png):
    path = tmp_path / "image.docx"
    _make_docx_with_image(path, tiny_png)
    report = da.analyze_docx_preservation(str(path))
    assert report["safe_to_convert_silently"] is False
    assert "images" in report["not_preservable_on_markdown_conversion"]
    assert report["image_count"] == 1


def test_docx_with_numbering_is_flagged(tmp_path):
    path = tmp_path / "numbered.docx"
    _make_docx_with_numbering(path)
    report = da.analyze_docx_preservation(str(path))
    assert "numbering" in report["not_preservable_on_markdown_conversion"]
    assert report["numbered_paragraph_count"] >= 2
    assert report["safe_to_convert_silently"] is False


def test_docx_with_comment_is_flagged(tmp_path):
    path = tmp_path / "commented.docx"
    _make_docx_with_comment(path)
    report = da.analyze_docx_preservation(str(path))
    assert report["has_comments"] is True
    assert "comments" in report["not_preservable_on_markdown_conversion"]
    assert report["safe_to_convert_silently"] is False


def test_not_a_docx_raises_a_declared_error(tmp_path):
    fake = tmp_path / "not_really.docx"
    fake.write_bytes(b"this is not a zip file at all")
    with pytest.raises(da.DocumentActionError):
        da.analyze_docx_preservation(str(fake))


# ---- route wiring ---------------------------------------------------------

def test_analyze_docx_route_reports_before_any_import(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.document import document_routes as routes

    path = tmp_path / "commented.docx"
    _make_docx_with_comment(path)

    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock(), upload_handler=MagicMock()))
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)
    with TestClient(app) as client:
        with open(path, "rb") as fh:
            resp = client.post("/api/documents/analyze-docx",
                               files={"file": ("commented.docx", fh,
                                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["safe_to_convert_silently"] is False
        assert "comments" in body["not_preservable_on_markdown_conversion"]
