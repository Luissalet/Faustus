"""L66/IDX-04 — a scanned PDF must not read as "leído"; a bad attachment must
not take the rest of the batch down with it.

Three things pinned here:

  1. `document_processor.pdf_ingestion_signal` turns the bounded PDF preview
     into the queued/uploading/extracting/ready/partial/failed vocabulary —
     a scanned page (no text anywhere) and a "cover page only" PDF both come
     back `partial: True` with an explicit `reason`, never `ready`; a
     corrupted/encrypted/oversized file comes back `failed` with a reason,
     never silently `ready` either.
  2. `upload_handler.save_upload` actually calls it for a PDF upload and
     carries `status`/`partial`/`partial_reason` on the returned metadata —
     the wiring the acceptance asks for, not just the function existing.
  3. one broken attachment (an office document whose optional converter
     blows up) leaves a friendly per-file failure banner in the message but
     does not stop the rest of the batch — a second, healthy attachment in
     the same turn still comes through.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# `_fake_reader` below stubs sys.modules['pypdf'] with a bare SimpleNamespace
# so `_process_pdf`'s `from pypdf import PdfReader` sees fake pages instead
# of a real parse. Its sibling `from pypdf.errors import PdfReadError` needs
# `pypdf.errors` to already be a real, importable submodule when that stub
# lands: importing it for real here, once, up front (before any stubbing)
# is what makes this file's outcome independent of whatever other test
# module happened to run before it and warm that cache — the same
# environment quirk `tests/test_pdf_extraction_budget.py` relies on run
# order (not this file's own choice) to avoid.
import pypdf.errors  # noqa: F401

from src import document_processor as dp
from src.upload_handler import UploadHandler


class _Page:
    def __init__(self, text=""):
        self.extract_text = Mock(return_value=text)
        self.images = []


def _fake_reader(monkeypatch, pages):
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=pages)))


# ── 1. pdf_ingestion_signal classification ──────────────────────────────────


def test_a_fully_scanned_pdf_is_partial_not_ready(monkeypatch):
    _fake_reader(monkeypatch, [_Page(""), _Page("")])
    signal = dp.pdf_ingestion_signal("scan.pdf")
    assert signal["status"] == "partial"
    assert signal["partial"] is True
    assert signal["reason"] == "scanned"
    assert signal["text_pages"] == 0
    assert signal["needs_ocr"] is True


def test_a_cover_page_only_pdf_is_partial_not_ready(monkeypatch):
    _fake_reader(monkeypatch, [_Page("Table of contents, title page"), _Page(""), _Page("")])
    signal = dp.pdf_ingestion_signal("cover-only.pdf")
    assert signal["status"] == "partial"
    assert signal["partial"] is True
    assert signal["reason"] == "cover_only"
    assert signal["pages"] == 3
    assert signal["text_pages"] == 1


def test_a_pdf_with_text_on_every_page_is_ready(monkeypatch):
    _fake_reader(monkeypatch, [_Page("first page prose"), _Page("second page prose")])
    signal = dp.pdf_ingestion_signal("normal.pdf")
    assert signal == {"status": "ready", "partial": False, "reason": None,
                      "pages": 2, "text_pages": 2, "needs_ocr": False}


def test_a_single_readable_page_is_ready_not_cover_only(monkeypatch):
    """cover_only means "more pages exist and only the first had text" — a
    genuinely one-page PDF that reads fine must not be misclassified."""
    _fake_reader(monkeypatch, [_Page("the whole document")])
    signal = dp.pdf_ingestion_signal("one-pager.pdf")
    assert signal["status"] == "ready"


@pytest.mark.parametrize("banner,reason", [
    ("\n\n[PDF too large to process inline: x.pdf (1 bytes, limit 2 bytes).]", "too_large"),
    ("\n\n[PDF appears corrupted and could not be parsed: x.pdf (bad xref)]", "corrupted"),
    ("\n\n[PDF is password-protected and could not be read: x.pdf. Provide the password.]", "encrypted"),
    ("\n\n[PDF processing failed: boom]", "unreadable"),
])
def test_unreadable_pdfs_are_failed_never_ready(monkeypatch, banner, reason):
    monkeypatch.setattr(dp, "_process_pdf", lambda path, owner=None, evidence_out=None, allow_vision=True: banner)
    signal = dp.pdf_ingestion_signal("x.pdf")
    assert signal["status"] == "failed"
    assert signal["partial"] is False
    assert signal["reason"] == reason


def test_ingestion_signal_never_triggers_vision_ocr_calls(monkeypatch):
    """This runs once per upload; it must stay a cheap text-only pass, never
    a per-image VL model call (that already happens, bounded, at chat time)."""
    called = []
    monkeypatch.setattr(dp, "analyze_image_with_vl", lambda *a, **kw: called.append(1) or "text")
    img = SimpleNamespace(image=SimpleNamespace(save=Mock()))
    page = _Page("")
    page.images = [img]
    _fake_reader(monkeypatch, [page])
    dp.pdf_ingestion_signal("scan.pdf")
    assert called == []


# ── 2. upload_handler.save_upload wiring ────────────────────────────────────


class _FakeUploadFile:
    def __init__(self, data: bytes, filename: str):
        import io
        self.file = io.BytesIO(data)
        self.filename = filename


def test_save_upload_carries_the_pdf_ingestion_status(tmp_path, monkeypatch):
    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    monkeypatch.setattr(
        "src.document_processor.pdf_ingestion_signal",
        lambda path, owner=None: {"status": "partial", "partial": True, "reason": "scanned",
                                  "pages": 5, "text_pages": 0, "needs_ocr": True},
    )
    upload = _FakeUploadFile(b"%PDF-1.4 not a real parse target\n%%EOF", "scan.pdf")

    meta = handler.save_upload(upload, client_ip="127.0.0.1", owner="carol")

    assert meta["mime"] == "application/pdf"
    assert meta["status"] == "partial"
    assert meta["partial"] is True
    assert meta["partial_reason"] == "scanned"
    assert meta["pages"] == 5

    # And persisted to the index, not just returned once.
    index = json.loads((tmp_path / "uploads" / "uploads.json").read_text())
    stored = next(iter(index.values()))
    assert stored["status"] == "partial"


def test_save_upload_marks_a_readable_pdf_ready(tmp_path, monkeypatch):
    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    monkeypatch.setattr(
        "src.document_processor.pdf_ingestion_signal",
        lambda path, owner=None: {"status": "ready", "partial": False, "reason": None,
                                  "pages": 2, "text_pages": 2, "needs_ocr": False},
    )
    upload = _FakeUploadFile(b"%PDF-1.4 whatever\n%%EOF", "report.pdf")

    meta = handler.save_upload(upload, client_ip="127.0.0.1", owner="carol")

    assert meta["status"] == "ready"
    assert meta["partial"] is False


def test_a_non_pdf_upload_is_ready_immediately_no_signal_call(tmp_path, monkeypatch):
    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    def _boom(*a, **kw):
        raise AssertionError("pdf_ingestion_signal must not run for a non-PDF upload")
    monkeypatch.setattr("src.document_processor.pdf_ingestion_signal", _boom)
    upload = _FakeUploadFile(b"hello world\n", "notes.txt")

    meta = handler.save_upload(upload, client_ip="127.0.0.1", owner="carol")

    assert meta["status"] == "ready"
    assert meta["partial"] is False


# ── 3. one bad attachment does not block the rest of the batch ─────────────


def test_a_broken_office_attachment_does_not_stop_the_rest_of_the_batch(tmp_path, monkeypatch):
    docx_path = tmp_path / "bad.docx"
    docx_path.write_bytes(b"not really a docx")
    txt_path = tmp_path / "good.txt"
    txt_path.write_text("the actual message content", encoding="utf-8")

    def _boom(*a, **kw):
        raise RuntimeError("markitdown blew up on this file")
    monkeypatch.setattr(dp, "_process_office_document", _boom)

    class _Handler:
        def is_image_file(self, *_a, **_k):
            return False

        def is_audio_file(self, *_a, **_k):
            return False

        def is_document_file(self, name, mime):
            return True

        def _inside_upload_dir(self, path):
            return True

    resolved = {
        "docx": {"id": "docx", "path": str(docx_path), "mime":
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "name": "bad.docx"},
        "txt": {"id": "txt", "path": str(txt_path), "mime": "text/plain", "name": "good.txt"},
    }

    content = dp.build_user_content(
        "hello", ["docx", "txt"], str(tmp_path), _Handler(),
        resolved_uploads=resolved,
    )

    assert "Failed to process attachment" in content
    assert "bad.docx" in content
    assert "the actual message content" in content
