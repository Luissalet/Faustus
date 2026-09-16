"""tests/test_r4_pdf_ops.py — R4 (Reach wave): PDF operations.

Every PDF used here is generated in-test with reportlab (no binary
fixtures, per the contract). `tmp_path` lands under the system temp dir,
which `src.tool_execution._resolve_tool_path` already allows by default, so
these exercise the REAL path-confinement guard, not a bypass of it.
"""
from __future__ import annotations

import os
import shutil

import pytest
from reportlab.pdfgen import canvas

from src import pdf_ops


def _make_pdf(path: str, n_pages: int = 3, label: str = "page") -> str:
    c = canvas.Canvas(str(path))
    for i in range(n_pages):
        c.drawString(72, 700, f"{label} {i + 1}")
        c.showPage()
    c.save()
    return str(path)


# ---------------------------------------------------------------------------
# page_count
# ---------------------------------------------------------------------------

def test_page_count(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf", 4)
    result = pdf_ops.page_count(src)
    assert result["page_count"] == 4


def test_page_count_missing_file(tmp_path):
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.page_count(str(tmp_path / "nope.pdf"))


# ---------------------------------------------------------------------------
# merge (delegates to src.document_actions.merge_pdfs)
# ---------------------------------------------------------------------------

def test_merge_two_pdfs(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", 2)
    b = _make_pdf(tmp_path / "b.pdf", 3)
    result = pdf_ops.merge([a, b], None)
    assert result["page_count"] == 5
    assert os.path.isfile(result["output"])
    assert result["output"] != a and result["output"] != b


def test_merge_requires_two_inputs(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", 2)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.merge([a], None)


# ---------------------------------------------------------------------------
# split (range-based, distinct from document_actions.split_pdf)
# ---------------------------------------------------------------------------

def test_split_by_ranges(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 10)
    result = pdf_ops.split(src, ["1-3", "4-6", "7-10"])
    assert len(result["outputs"]) == 3
    for out in result["outputs"]:
        assert os.path.isfile(out)
    assert pdf_ops.page_count(result["outputs"][0])["page_count"] == 3
    assert pdf_ops.page_count(result["outputs"][2])["page_count"] == 4


def test_split_out_of_range(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.split(src, ["1-9"])


# ---------------------------------------------------------------------------
# extract_pages
# ---------------------------------------------------------------------------

def test_extract_pages(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 6)
    result = pdf_ops.extract_pages(src, "2,4-5")
    assert result["page_count"] == 3
    assert pdf_ops.page_count(result["output"])["page_count"] == 3


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------

def test_rotate_all_pages(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    result = pdf_ops.rotate(src, None, 90)
    assert result["rotated_pages"] == [1, 2]
    reader = pdf_ops._open_reader(result["output"])
    assert reader.pages[0].rotation == 90


def test_rotate_rejects_non_multiple_of_90(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.rotate(src, None, 45)


# ---------------------------------------------------------------------------
# reorder
# ---------------------------------------------------------------------------

def test_reorder_full_permutation(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    result = pdf_ops.reorder(src, [3, 1, 2])
    assert result["page_count"] == 3


def test_reorder_rejects_partial_order(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.reorder(src, [1, 2])


# ---------------------------------------------------------------------------
# delete_pages
# ---------------------------------------------------------------------------

def test_delete_pages(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 5)
    result = pdf_ops.delete_pages(src, "2,4")
    assert result["page_count"] == 3
    assert result["deleted_pages"] == [2, 4]


def test_delete_all_pages_refused(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.delete_pages(src, "1-2")


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def test_metadata_read_default(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    result = pdf_ops.metadata(src)
    assert "metadata" in result
    assert "output" not in result


def test_metadata_write(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    result = pdf_ops.metadata(src, {"title": "Hola", "author": "Luis"})
    assert result["metadata"]["title"] == "Hola"
    assert result["metadata"]["author"] == "Luis"
    reread = pdf_ops.metadata(result["output"])
    assert reread["metadata"]["title"] == "Hola"


def test_metadata_unknown_field_rejected(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.metadata(src, {"bogus": "x"})


# ---------------------------------------------------------------------------
# compress
# ---------------------------------------------------------------------------

def test_compress_reports_before_after(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 5)
    result = pdf_ops.compress(src)
    assert result["bytes_before"] > 0
    assert result["bytes_after"] > 0
    assert "bytes_saved" in result and "percent_saved" in result
    assert os.path.isfile(result["output"])


# ---------------------------------------------------------------------------
# watermark_text
# ---------------------------------------------------------------------------

def test_watermark_text(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    result = pdf_ops.watermark_text(src, "DRAFT")
    assert result["page_count"] == 2
    assert os.path.isfile(result["output"])


def test_watermark_text_requires_text(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.watermark_text(src, "")


# ---------------------------------------------------------------------------
# overwrite protection
# ---------------------------------------------------------------------------

def test_output_never_clobbers_input_without_overwrite(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.rotate(src, None, 90, output=src)


def test_output_may_clobber_input_with_overwrite(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    result = pdf_ops.rotate(src, None, 90, output=src, overwrite=True)
    assert result["output"] == pdf_ops.resolve_path(src)


# ---------------------------------------------------------------------------
# path confinement — the whole point of routing through resolve_path
# ---------------------------------------------------------------------------

def test_path_outside_allowlist_rejected(tmp_path, monkeypatch):
    # /etc is never in the default allowlist (DATA_DIR + tmp dirs).
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.resolve_path("/etc/passwd")


# ---------------------------------------------------------------------------
# to_images — lazy optional dependency, monkeypatched absent/present
# ---------------------------------------------------------------------------

def test_to_images_missing_dependency_gives_clear_error(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("pypdfium2", "pdf2image"):
            raise ImportError(f"no {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(pdf_ops.PdfOpsError, match="pypdfium2 or pdf2image"):
        pdf_ops.to_images(src)


# ---------------------------------------------------------------------------
# ocr — lazy optional external CLI, monkeypatched via shutil.which
# ---------------------------------------------------------------------------

def test_ocr_missing_binary_gives_clear_error(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pdf_ops.PdfOpsError, match="ocrmypdf"):
        pdf_ops.ocr(src)


def test_ocr_runs_subprocess_without_shell(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    calls = {}

    def fake_which(name):
        return "/usr/bin/ocrmypdf" if name == "ocrmypdf" else None

    class FakeCompleted:
        returncode = 0
        stdout = "OCR done"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        # ocrmypdf would normally write the output file; simulate that.
        out_path = cmd[-1]
        shutil.copyfile(src, out_path)
        return FakeCompleted()

    monkeypatch.setattr(shutil, "which", fake_which)
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = pdf_ops.ocr(src)
    assert result["output"]
    assert calls["cmd"][0] == "/usr/bin/ocrmypdf"
    assert "shell" not in calls["kwargs"]  # never shell=True
    assert isinstance(calls["cmd"], list)
