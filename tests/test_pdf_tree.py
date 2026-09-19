"""tests/test_pdf_tree.py — structural (table-of-contents) PDF navigation.

Every PDF used here is generated in-test with pypdf/reportlab (no binary
fixtures, per the contract `tests/test_r4_pdf_ops.py` already follows).
`tmp_path` lands under the system temp dir, which
`src.tool_execution._resolve_tool_path` already allows by default, so these
exercise the REAL path-confinement guard, not a bypass of it.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from reportlab.pdfgen import canvas

from src import pdf_tree
from src.agent_tools.pdf_tree_tool import PdfFindSectionTool, PdfOutlineTool, PdfReadSectionTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _body_pdf(path: str, n_pages: int, label: str = "Lorem ipsum dolor sit amet.") -> str:
    """Plain PDF with generic body text — no bookmarks, no heading-like lines."""
    c = canvas.Canvas(str(path))
    for i in range(n_pages):
        c.drawString(72, 700, f"{label} (page {i + 1})")
        c.drawString(72, 680, "consectetur adipiscing elit, sed do eiusmod.")
        c.showPage()
    c.save()
    return str(path)


def _outline_pdf(path: str, n_pages: int = 10) -> str:
    """PDF with nested bookmarks: Chapter 1 (pp.1-5) > Sec 1.1 (pp.2-3),
    Sec 1.2 (pp.4-5); Chapter 2 (pp.6-10)."""
    import pypdf

    src = _body_pdf(str(path) + ".src.pdf", n_pages)
    reader = pypdf.PdfReader(src)
    writer = pypdf.PdfWriter()
    writer.append(reader)
    ch1 = writer.add_outline_item("Chapter 1", 0)
    writer.add_outline_item("Sec 1.1", 1, parent=ch1)
    writer.add_outline_item("Sec 1.2", 3, parent=ch1)
    writer.add_outline_item("Chapter 2", 5)
    with open(path, "wb") as fh:
        writer.write(fh)
    return str(path)


def _heading_pdf(path: str) -> str:
    """PDF with numbered-section heading text and no outline: page1 "1.
    Introduction", page2 "1.1 Background", page3 "1.2 Scope", page4 "2.
    Methods"."""
    c = canvas.Canvas(str(path))

    def page(lines):
        y = 700
        for ln in lines:
            c.drawString(72, y, ln)
            y -= 20
        c.showPage()

    page(["1. Introduction", "Some body text here.", "More body text."])
    page(["1.1 Background", "Body text for background."])
    page(["1.2 Scope", "Body text for scope."])
    page(["2. Methods", "Body text for methods."])
    c.save()
    return str(path)


# ---------------------------------------------------------------------------
# build_tree: outline source
# ---------------------------------------------------------------------------

def test_build_tree_from_outline(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tree = pdf_tree.build_tree(path)
    assert tree["source"] == "outline"
    assert tree["pages"] == 10
    assert [n["title"] for n in tree["nodes"]] == ["Chapter 1", "Chapter 2"]

    ch1, ch2 = tree["nodes"]
    assert ch1["id"] == "1" and ch1["level"] == 1
    assert ch1["start_page"] == 1
    assert ch1["end_page"] == 5           # Chapter 2 starts at page 6
    assert [c["title"] for c in ch1["children"]] == ["Sec 1.1", "Sec 1.2"]

    sec11, sec12 = ch1["children"]
    assert sec11["id"] == "1.1" and sec11["start_page"] == 2 and sec11["end_page"] == 3
    assert sec12["id"] == "1.2" and sec12["start_page"] == 4 and sec12["end_page"] == 5

    assert ch2["id"] == "2" and ch2["start_page"] == 6 and ch2["end_page"] == 10
    assert ch2["children"] == []


# ---------------------------------------------------------------------------
# build_tree: headings source (fallback, no usable outline)
# ---------------------------------------------------------------------------

def test_build_tree_from_headings(tmp_path):
    pdf_tree.clear_cache()
    path = _heading_pdf(tmp_path / "headings.pdf")
    tree = pdf_tree.build_tree(path)
    assert tree["source"] == "headings"
    assert tree["pages"] == 4
    assert [n["title"] for n in tree["nodes"]] == ["1. Introduction", "2. Methods"]

    intro, methods = tree["nodes"]
    assert intro["id"] == "1" and intro["level"] == 1
    assert intro["start_page"] == 1 and intro["end_page"] == 3
    assert [c["title"] for c in intro["children"]] == ["1.1 Background", "1.2 Scope"]
    bg, scope = intro["children"]
    assert bg["id"] == "1.1" and bg["start_page"] == 2 and bg["end_page"] == 2
    assert scope["id"] == "1.2" and scope["start_page"] == 3 and scope["end_page"] == 3

    assert methods["id"] == "2" and methods["start_page"] == 4 and methods["end_page"] == 4


# ---------------------------------------------------------------------------
# build_tree: pages source (last resort, neither outline nor headings)
# ---------------------------------------------------------------------------

def test_build_tree_from_pages_fallback(tmp_path):
    pdf_tree.clear_cache()
    path = _body_pdf(tmp_path / "plain.pdf", 25)
    tree = pdf_tree.build_tree(path)
    assert tree["source"] == "pages"
    assert tree["pages"] == 25
    assert len(tree["nodes"]) == 3
    assert tree["nodes"][0]["title"] == "pages 1-10"
    assert tree["nodes"][0]["start_page"] == 1 and tree["nodes"][0]["end_page"] == 10
    assert tree["nodes"][1]["title"] == "pages 11-20"
    assert tree["nodes"][2]["title"] == "pages 21-25"
    assert tree["nodes"][2]["end_page"] == 25
    assert all(n["children"] == [] for n in tree["nodes"])
    assert [n["id"] for n in tree["nodes"]] == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# read_section
# ---------------------------------------------------------------------------

def test_read_section_returns_only_that_nodes_pages(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    section = pdf_tree.read_section(path, "1.2")
    assert section["id"] == "1.2"
    assert section["title"] == "Sec 1.2"
    assert section["start_page"] == 4 and section["end_page"] == 5
    assert "[page 4]" in section["text"]
    assert "[page 5]" in section["text"]
    assert "[page 1]" not in section["text"]
    assert "[page 6]" not in section["text"]
    assert section["truncated"] is False


def test_read_section_clips_to_max_chars(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    section = pdf_tree.read_section(path, "2", max_chars=20)
    assert section["truncated"] is True
    assert len(section["text"]) <= 20
    assert "stopped" in section["note"]


def test_read_section_bad_node_id_raises(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    with pytest.raises(pdf_tree.PdfTreeError):
        pdf_tree.read_section(path, "9.9.9")


def test_read_section_missing_node_id_raises(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    with pytest.raises(pdf_tree.PdfTreeError):
        pdf_tree.read_section(path, "")


# ---------------------------------------------------------------------------
# find_in_tree
# ---------------------------------------------------------------------------

def test_find_in_tree_matches_titles(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    matches = pdf_tree.find_in_tree(path, "1.2")
    assert matches
    assert matches[0]["id"] == "1.2"
    assert matches[0]["title"] == "Sec 1.2"


def test_find_in_tree_no_match(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    matches = pdf_tree.find_in_tree(path, "nonexistent gibberish query")
    assert matches == []


# ---------------------------------------------------------------------------
# Path confinement — same guard as pdf_ops.
# ---------------------------------------------------------------------------

def test_build_tree_path_outside_workspace_refused():
    with pytest.raises(pdf_tree.PdfTreeError):
        pdf_tree.build_tree("/etc/passwd")


def test_read_section_path_outside_workspace_refused():
    with pytest.raises(pdf_tree.PdfTreeError):
        pdf_tree.read_section("/etc/shadow", "1")


# ---------------------------------------------------------------------------
# Cache: in-process LRU keyed by (path, mtime, size); invalidates on change.
# ---------------------------------------------------------------------------

def test_cache_invalidates_on_file_change(tmp_path):
    pdf_tree.clear_cache()
    path = str(tmp_path / "cached.pdf")
    _body_pdf(path, 5)
    tree1 = pdf_tree.build_tree(path)
    assert tree1["pages"] == 5

    time.sleep(0.05)  # ensure a distinct mtime on filesystems with 1s/10ms resolution
    os.utime(path, None)
    _body_pdf(path, 8)  # rewrite the same path with a different page count
    tree2 = pdf_tree.build_tree(path)
    assert tree2["pages"] == 8
    assert tree2 is not tree1


def test_cache_hits_for_unchanged_file(tmp_path):
    pdf_tree.clear_cache()
    path = str(tmp_path / "same.pdf")
    _body_pdf(path, 3)
    tree1 = pdf_tree.build_tree(path)
    tree2 = pdf_tree.build_tree(path)
    assert tree1 is tree2


# ---------------------------------------------------------------------------
# Tool handlers wired — same as tests/test_r4_pdf_ops_tool.py's coverage.
# ---------------------------------------------------------------------------

async def test_pdf_outline_tool(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfOutlineTool()
    result = await tool.execute(json.dumps({"path": path}), {})
    assert result["exit_code"] == 0
    assert result["source"] == "outline"
    assert result["pages"] == 10
    assert "Chapter 1" in result["output"]
    assert "1.1" in result["output"]


async def test_pdf_outline_tool_max_depth(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfOutlineTool()
    result = await tool.execute(json.dumps({"path": path, "max_depth": 1}), {})
    assert result["exit_code"] == 0
    assert "Chapter 1" in result["output"]
    assert "Sec 1.1" not in result["output"]


async def test_pdf_read_section_tool(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfReadSectionTool()
    result = await tool.execute(json.dumps({"path": path, "node_id": "1.1"}), {})
    assert result["exit_code"] == 0
    assert result["id"] == "1.1"
    assert "[page 2]" in result["output"]


async def test_pdf_read_section_tool_missing_node_id(tmp_path):
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfReadSectionTool()
    result = await tool.execute(json.dumps({"path": path}), {})
    assert result["exit_code"] == 1
    assert "node_id" in result["error"]


async def test_pdf_read_section_tool_bad_node_id(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfReadSectionTool()
    result = await tool.execute(json.dumps({"path": path, "node_id": "9.9"}), {})
    assert result["exit_code"] == 1
    assert result["error_class"] == "pdf_tree.invalid"


async def test_pdf_find_section_tool(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfFindSectionTool()
    result = await tool.execute(json.dumps({"path": path, "query": "Chapter 2"}), {})
    assert result["exit_code"] == 0
    assert result["matches"]
    assert result["matches"][0]["id"] == "2"


async def test_pdf_find_section_tool_no_match(tmp_path):
    pdf_tree.clear_cache()
    path = _outline_pdf(tmp_path / "outline.pdf")
    tool = PdfFindSectionTool()
    result = await tool.execute(json.dumps({"path": path, "query": "zzz nonexistent"}), {})
    assert result["exit_code"] == 0
    assert result["matches"] == []


async def test_pdf_outline_tool_path_outside_workspace(tmp_path):
    tool = PdfOutlineTool()
    result = await tool.execute(json.dumps({"path": "/etc/passwd"}), {})
    assert result["exit_code"] == 1
    assert result["error_class"] == "pdf_tree.invalid"



def test_outline_titles_are_whitespace_normalised(tmp_path):
    from pypdf import PdfWriter
    pdf_tree.clear_cache()
    path = str(tmp_path / "nl.pdf")
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    writer.add_outline_item("1.\r\nIntroduction  ", 0)
    with open(path, "wb") as fh:
        writer.write(fh)
    tree = pdf_tree.build_tree(path)
    assert tree["nodes"][0]["title"] == "1. Introduction"
