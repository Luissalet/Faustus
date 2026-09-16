"""tests/test_r4_pdf_ops_tool.py — R4: the `pdf_ops` agent tool wrapper."""
from __future__ import annotations

import json
import os

import pytest
from reportlab.pdfgen import canvas

from src.agent_tools.pdf_ops_tool import PdfOpsTool


def _make_pdf(path: str, n_pages: int = 3) -> str:
    c = canvas.Canvas(str(path))
    for i in range(n_pages):
        c.drawString(72, 700, f"page {i + 1}")
        c.showPage()
    c.save()
    return str(path)


async def test_page_count_via_tool(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf", 3)
    tool = PdfOpsTool()
    result = await tool.execute(json.dumps({"op": "page_count", "input": src}), {})
    assert result["exit_code"] == 0
    assert result["page_count"] == 3


async def test_missing_op_errors(tmp_path):
    tool = PdfOpsTool()
    result = await tool.execute(json.dumps({}), {})
    assert result["exit_code"] == 1
    assert "op" in result["error"]


async def test_unknown_op_errors(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf", 1)
    tool = PdfOpsTool()
    result = await tool.execute(json.dumps({"op": "frobnicate", "input": src}), {})
    assert result["exit_code"] == 1
    assert "unknown op" in result["error"]


async def test_merge_via_tool(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", 2)
    b = _make_pdf(tmp_path / "b.pdf", 2)
    tool = PdfOpsTool()
    result = await tool.execute(json.dumps({"op": "merge", "inputs": [a, b]}), {})
    assert result["exit_code"] == 0
    assert result["page_count"] == 4
    assert os.path.isfile(result["output"])


async def test_rotate_missing_degrees_errors(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf", 1)
    tool = PdfOpsTool()
    result = await tool.execute(json.dumps({"op": "rotate", "input": src}), {})
    assert result["exit_code"] == 1
    assert "degrees" in result["error"]


async def test_watermark_via_tool(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf", 1)
    tool = PdfOpsTool()
    result = await tool.execute(
        json.dumps({"op": "watermark_text", "input": src, "text": "DRAFT"}), {}
    )
    assert result["exit_code"] == 0
    assert os.path.isfile(result["output"])


async def test_path_outside_workspace_refused(tmp_path):
    tool = PdfOpsTool()
    result = await tool.execute(
        json.dumps({"op": "page_count", "input": "/etc/passwd"}), {}
    )
    assert result["exit_code"] == 1
    assert result["error_class"] == "pdf_ops.invalid"
