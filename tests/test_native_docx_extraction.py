import zipfile
import pytest

from src import markitdown_runtime as runtime


def test_native_docx_retains_heading_without_optional_converter(tmp_path, monkeypatch):
    Document = pytest.importorskip("docx").Document
    doc = Document()
    doc.add_heading("Project context", level=2)
    doc.add_paragraph("A useful fact.")
    path = tmp_path / "source.docx"
    doc.save(path)
    def unavailable():
        raise RuntimeError("optional converter absent")
    monkeypatch.setattr(runtime, "load_markitdown", unavailable)
    assert runtime.convert_to_markdown(str(path)) == "## Project context\n\nA useful fact."


def test_native_docx_checks_inflated_size_before_reading_member(tmp_path, monkeypatch):
    path = tmp_path / "oversized.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 1025)
    monkeypatch.setattr(runtime, "MAX_NATIVE_DOCX_XML_BYTES", 1024)
    def no_read(*args, **kwargs):
        raise AssertionError("oversized XML was opened")
    monkeypatch.setattr(zipfile.ZipFile, "open", no_read)
    assert runtime._extract_docx_native(str(path)) is None
