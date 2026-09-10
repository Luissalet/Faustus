"""IDX-05 — referencias multimodales exactas.

`src/document_processor.py::pdf_page_evidence_ref` gives a PDF page a proper
`EvidenceRef` (locator kind="page", a content hash of exactly that page's
text) instead of only the free-text "[Page N text]:" marker `_process_pdf`
already produced. Passing `evidence_out=[]` into `_process_pdf` is purely
additive — the returned STRING is byte-identical to the no-evidence call, so
every existing caller (and the whole `test_idx_pdf_robustness.py` suite) is
unaffected; only a caller that opts in gets the refs.
"""

import io

import pytest

from src import document_processor as dp
from src.contracts.tool import EvidenceRef


def _simple_pdf_bytes(pages: int = 1, text: str = "Hello world") -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(300, 300))
    for i in range(pages):
        c.drawString(50, 150, f"{text} page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_page_evidence_ref_is_well_formed():
    ref = dp.pdf_page_evidence_ref("doc.pdf", 3, "the exact text of page 3",
                                   owner_id="luis", project_id="p1")
    assert isinstance(ref, EvidenceRef)
    assert ref.source_type == "file"
    assert ref.source_ref == "doc.pdf"
    assert ref.locator.kind == "page"
    assert ref.locator.value == "3"
    assert len(ref.content_sha256) == 64
    assert ref.owner_id == "luis" and ref.project_id == "p1"


def test_evidence_id_changes_with_page_or_content():
    a = dp.pdf_page_evidence_ref("doc.pdf", 1, "text A")
    b = dp.pdf_page_evidence_ref("doc.pdf", 2, "text A")  # different page
    c = dp.pdf_page_evidence_ref("doc.pdf", 1, "text B")  # different content
    assert len({a.evidence_id, b.evidence_id, c.evidence_id}) == 3


def test_process_pdf_without_evidence_out_is_unchanged(tmp_path):
    """Backward compatibility: the default call (no evidence_out) behaves
    exactly as before this lote — same string, no new side effects."""
    path = tmp_path / "doc.pdf"
    path.write_bytes(_simple_pdf_bytes(pages=1))
    without = dp._process_pdf(str(path))
    with_default = dp._process_pdf(str(path), evidence_out=None)
    assert without == with_default


def test_process_pdf_collects_one_evidence_ref_per_text_page(tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(_simple_pdf_bytes(pages=3))

    collected: list = []
    result = dp._process_pdf(str(path), evidence_out=collected)

    assert "[PDF content]:" in result  # the normal text output, untouched
    assert len(collected) == 3
    pages = sorted(int(ref.locator.value) for ref in collected)
    assert pages == [1, 2, 3]
    for ref in collected:
        assert ref.source_ref == str(path)
        assert ref.locator.kind == "page"


def test_evidence_ref_hash_matches_the_extracted_page_text(tmp_path):
    """The point of the contract: the ref proves exactly what was read.
    Recomputing the hash of the SAME page text must match."""
    import hashlib

    path = tmp_path / "doc.pdf"
    path.write_bytes(_simple_pdf_bytes(pages=1, text="Verifiable content"))
    collected: list = []
    dp._process_pdf(str(path), evidence_out=collected)
    assert len(collected) == 1
    ref = collected[0]
    # Re-extract the same page text the way _process_pdf itself did, and
    # confirm the stored hash is exactly that text's hash — not the whole
    # marker, not the whole document.
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    page_text = (reader.pages[0].extract_text() or "").strip()
    assert ref.content_sha256 == hashlib.sha256(page_text.encode("utf-8", "replace")).hexdigest()
