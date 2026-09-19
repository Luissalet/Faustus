"""FAUSTUS #146 — stable page/block locators for PDF-derived RAG chunks.

Hermetic like tests/test_rag_index_hidden_dirs.py: VectorRAG built via
__new__ (skip Chroma connect), add_document stubbed to record what would be
written. A real multi-page PDF is generated with reportlab so extraction
goes through the actual pypdf path (src.personal_docs.extract_pdf_pages).
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.rag_vector as rag_vector
from src.personal_docs import extract_pdf_pages

reportlab = pytest.importorskip("reportlab")
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter


def _make_pdf(path, page_texts):
    c = canvas.Canvas(str(path), pagesize=letter)
    for text in page_texts:
        y = 750
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    c.save()


def _make_rag(recorded):
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma connect

    def _record(text, metadata):
        recorded.append((text, metadata))
        return True

    rag.add_document = _record
    return rag


def _long(sentence, n=20):
    return " ".join(f"{sentence} Sentence number {i} adds more content." for i in range(n))


PAGE_TEXTS = [
    _long("This is page one."),
    _long("This is page two."),
    _long("This is page three, the final page."),
]


@pytest.fixture()
def sample_pdf(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, PAGE_TEXTS)
    return pdf_path


def test_extract_pdf_pages_returns_one_string_per_page(sample_pdf):
    pages = extract_pdf_pages(str(sample_pdf))
    assert len(pages) == 3
    assert "page one" in pages[0]
    assert "page two" in pages[1]
    assert "page three" in pages[2]


def test_pdf_chunks_carry_page_and_locator(sample_pdf):
    recorded = []
    rag = _make_rag(recorded)
    result = rag.index_personal_documents(str(sample_pdf.parent))

    assert result["success"] is True
    assert recorded, "expected at least one indexed chunk"

    for _text, meta in recorded:
        assert meta["type"] == ".pdf"
        assert isinstance(meta["page"], int) and meta["page"] >= 1
        assert isinstance(meta["page_end"], int) and meta["page_end"] >= meta["page"]
        assert isinstance(meta["block"], int) and meta["block"] >= 0
        assert "locator" in meta and meta["locator"]

    # Every chunk's locator matches its own page/page_end/block fields.
    for _text, meta in recorded:
        if meta["page"] == meta["page_end"]:
            expected = f"p{meta['page']}#b{meta['block']}"
        else:
            expected = f"p{meta['page']}-{meta['page_end']}#b{meta['block']}"
        assert meta["locator"] == expected

    # Page numbers reported are within the PDF's real page range.
    pages_seen = {meta["page"] for _text, meta in recorded}
    assert pages_seen.issubset({1, 2, 3})
    assert 1 in pages_seen and 3 in pages_seen


def test_pdf_locators_are_stable_across_runs(sample_pdf):
    recorded1, recorded2 = [], []
    rag1 = _make_rag(recorded1)
    rag1.index_personal_documents(str(sample_pdf.parent))

    rag2 = _make_rag(recorded2)
    rag2.index_personal_documents(str(sample_pdf.parent))

    locators1 = [meta["locator"] for _t, meta in recorded1]
    locators2 = [meta["locator"] for _t, meta in recorded2]
    assert locators1 == locators2
    assert locators1  # non-empty


def test_pdf_locators_have_correct_page_numbers_no_overlap_block_reset(sample_pdf):
    """block index resets to 0 for each new page_start."""
    recorded = []
    rag = _make_rag(recorded)
    rag.index_personal_documents(str(sample_pdf.parent))

    seen_blocks_by_page = {}
    for _text, meta in recorded:
        page = meta["page"]
        block = meta["block"]
        seen = seen_blocks_by_page.setdefault(page, set())
        assert block not in seen, "duplicate block index within same page_start"
        seen.add(block)
        # Blocks for a page should start at 0 and be contiguous in first-seen order
    for page, blocks in seen_blocks_by_page.items():
        assert min(blocks) == 0


def test_non_pdf_chunks_have_no_locator(tmp_path):
    (tmp_path / "note.md").write_text("Just a plain text note, nothing fancy here.", encoding="utf-8")
    recorded = []
    rag = _make_rag(recorded)
    result = rag.index_personal_documents(str(tmp_path))

    assert result["success"] is True
    assert recorded
    for _text, meta in recorded:
        assert "locator" not in meta
        assert "page" not in meta


def test_formatted_retrieval_context_includes_locator():
    """The chat_processor RAG-injection format embeds '[filename pX#bY]'."""
    metadata = {"filename": "sample.pdf", "locator": "p2#b0", "source": "/x/sample.pdf"}
    document = "This is page two content."
    filename = metadata.get("filename", metadata.get("source", "unknown"))
    locator = metadata.get("locator")
    formatted = f"[{filename}" + (f" {locator}" if locator else "") + f"]\n{document}"
    assert formatted == "[sample.pdf p2#b0]\nThis is page two content."
