"""read_file on a PDF, an Office document, a UTF-16 file or a binary: the
model gets text (or a plain "binary file" error), never raw bytes."""
from __future__ import annotations

import json

import pytest

from src.agent_tools.filesystem_tools import ReadFileTool

pytestmark = pytest.mark.asyncio


def _pdf(path, pages):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        c.drawString(72, 760, text)
        c.showPage()
    c.save()


async def _read(path, **extra):
    return await ReadFileTool().execute(json.dumps({"path": str(path), **extra}), {})


async def test_pdf_returns_its_text_not_its_bytes(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    target = tmp_path / "contrato.pdf"
    _pdf(target, ["SEPTIMA. Desistimiento con cuarenta y cinco dias", "OCTAVA. Mascotas"])
    res = await _read(target)
    assert res["exit_code"] == 0, res
    out = res["output"]
    assert "%PDF" not in out and "/Type /Font" not in out
    assert "cuarenta y cinco dias" in out and "Mascotas" in out
    assert "pdf_outline" in out


async def test_pdf_ranged_read_slices_the_text(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    target = tmp_path / "doc.pdf"
    _pdf(target, [f"linea {i}" for i in range(1, 6)])
    whole = (await _read(target))["output"]
    part = (await _read(target, offset=1, limit=2))["output"]
    assert len(part) < len(whole)


async def test_docx_is_converted(tmp_path):
    docx = pytest.importorskip("docx")
    target = tmp_path / "nota.docx"
    d = docx.Document()
    d.add_paragraph("Reunion de vecinos el martes")
    d.save(str(target))
    res = await _read(target)
    assert res["exit_code"] == 0, res
    assert "Reunion de vecinos el martes" in res["output"]
    assert "PK" not in res["output"][:40]


async def test_binary_file_is_named_not_dumped(tmp_path):
    target = tmp_path / "tool.bin"
    target.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00" + bytes(range(256)) * 4)
    res = await _read(target)
    assert res["exit_code"] == 0
    assert "binary file" in res["output"] and "\x90" not in res["output"]


async def test_utf16_text_is_decoded(tmp_path):
    target = tmp_path / "export.txt"
    target.write_bytes("Nombre;Importe\nAna;12,50\n".encode("utf-16"))
    res = await _read(target)
    assert res["exit_code"] == 0, res
    assert "Ana;12,50" in res["output"]


async def test_plain_text_unchanged(tmp_path):
    target = tmp_path / "notas.md"
    target.write_text("# Hola\nlinea dos\n", encoding="utf-8")
    res = await _read(target)
    assert res["exit_code"] == 0
    assert res["output"].startswith("# Hola")
    assert "revision" in res
