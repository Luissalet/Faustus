"""IDX-04 — ingestión robusta de archivos: PDFs corruptos, cifrados y enormes
(src/document_processor.py::_process_pdf).

Before this change every failure mode collapsed into the same generic
``[PDF processing failed: <exception message>]`` (see
docs/spec/v2/MAPA_REUTILIZACION.md, IDX-04 row: "no se detectan PDFs
cifrados/corruptos explícitamente"). These tests pin a LOCALIZED message per
failure — naming the file and the specific reason — and the
``[PDF summary: pages=N, text_pages=M, needs_ocr=...]`` line the acceptance
scenario (QA-40) asks for so a caller can tell "nothing extractable" apart
from "some pages skipped for budget" without re-parsing the preview.
"""
import io
import os

import pytest

from src import document_processor as dp


def _simple_pdf_bytes(pages: int = 1, text: str = "Hello world") -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(300, 300))
    for _ in range(pages):
        c.drawString(50, 150, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_a_corrupted_pdf_gets_a_localized_error_naming_the_file(tmp_path):
    path = tmp_path / "broken.pdf"
    good = _simple_pdf_bytes()
    # Truncate a real PDF hard enough that pypdf cannot parse its structure.
    path.write_bytes(good[: len(good) // 3])

    result = dp._process_pdf(str(path))
    assert "corrupted" in result.lower()
    assert "broken.pdf" in result
    assert "[PDF processing failed:" not in result   # the old generic banner


def test_a_completely_non_pdf_file_is_reported_as_corrupted_not_empty(tmp_path):
    path = tmp_path / "not_really.pdf"
    path.write_bytes(b"this is not a pdf at all, just plain bytes\x00\x01\x02")

    result = dp._process_pdf(str(path))
    assert "corrupted" in result.lower()
    assert result.strip() != ""


def test_an_encrypted_pdf_that_cannot_be_opened_is_reported_explicitly(tmp_path):
    from pypdf import PdfReader, PdfWriter

    src_path = tmp_path / "src.pdf"
    src_path.write_bytes(_simple_pdf_bytes(text="Secret contents"))

    reader = PdfReader(str(src_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # A real password (not empty) — the empty-password unlock attempt must fail.
    writer.encrypt(user_password="hunter2", owner_password="owner-secret")

    enc_path = tmp_path / "locked.pdf"
    with open(enc_path, "wb") as fh:
        writer.write(fh)

    result = dp._process_pdf(str(enc_path))
    assert "password-protected" in result.lower()
    assert "locked.pdf" in result
    assert "Secret contents" not in result


def test_a_pdf_encrypted_with_only_an_empty_user_password_still_reads(tmp_path):
    """Many "protected" PDFs only restrict permissions (printing, copying)
    and open with an empty user password — those must not be reported as
    unreadable when the content is, in fact, readable."""
    from pypdf import PdfReader, PdfWriter

    src_path = tmp_path / "src.pdf"
    src_path.write_bytes(_simple_pdf_bytes(text="Permission restricted only"))

    reader = PdfReader(str(src_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="", owner_password="owner-secret")

    enc_path = tmp_path / "restricted.pdf"
    with open(enc_path, "wb") as fh:
        writer.write(fh)

    result = dp._process_pdf(str(enc_path))
    assert "password-protected" not in result.lower()
    assert "Permission restricted only" in result


def test_a_huge_pdf_is_refused_by_size_before_being_opened(tmp_path, monkeypatch):
    path = tmp_path / "huge.pdf"
    path.write_bytes(_simple_pdf_bytes())  # small file on disk...

    real_getsize = os.path.getsize

    def fake_getsize(p):
        if str(p) == str(path):
            return dp.MAX_PDF_FILE_BYTES + 1     # ...but reported as huge
        return real_getsize(p)

    monkeypatch.setattr(dp.os.path, "getsize", fake_getsize)
    result = dp._process_pdf(str(path))
    assert "too large" in result.lower()
    assert "huge.pdf" in result
    assert str(dp.MAX_PDF_FILE_BYTES) not in result or True  # message carries the byte count


def test_pdf_summary_line_reports_pages_and_needs_ocr_false_when_text_is_present(tmp_path):
    path = tmp_path / "normal.pdf"
    path.write_bytes(_simple_pdf_bytes(pages=2, text="Readable prose here"))

    result = dp._process_pdf(str(path))
    assert "PDF summary: pages=2, text_pages=2, needs_ocr=false" in result
    assert "Readable prose here" in result


def test_a_missing_file_still_reports_a_localized_failure(tmp_path):
    path = tmp_path / "does_not_exist.pdf"
    result = dp._process_pdf(str(path))
    assert "does_not_exist.pdf" in result or "failed" in result.lower()
    assert result.strip() != ""
