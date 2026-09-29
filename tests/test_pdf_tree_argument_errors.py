"""Direct handlers must return a tool failure, not leak conversion exceptions."""
import pytest

from src import pdf_tree
from src.agent_tools.pdf_tree_tool import PdfFindSectionTool, PdfReadSectionTool


@pytest.mark.parametrize("bad", ["abc", [], {}, float("inf")])
@pytest.mark.parametrize("operation", ["find", "read"])
async def test_direct_numeric_argument_failure_never_reads_pdf(monkeypatch, bad, operation):
    def forbidden(*args, **kwargs):
        pytest.fail("Malformed numeric argument reached PDF backend")

    monkeypatch.setattr(pdf_tree, "read_section", forbidden)
    monkeypatch.setattr(pdf_tree, "find_in_tree", forbidden)
    if operation == "find":
        tool, args = PdfFindSectionTool(), {"path": "unused.pdf", "query": "chapter", "limit": bad}
    else:
        tool, args = PdfReadSectionTool(), {"path": "unused.pdf", "node_id": "1", "max_chars": bad}
    result = await tool.execute(args, {})
    assert result["exit_code"] == 1
    assert result["error_class"] == "pdf_tree.error"
    assert "error" in result


@pytest.mark.parametrize("limit", [1, 8, 20])
async def test_valid_find_limit_reaches_backend_unchanged(monkeypatch, limit):
    calls = []

    def find(path, query, **kwargs):
        calls.append((path, query, kwargs))
        return []

    monkeypatch.setattr(pdf_tree, "find_in_tree", find)
    result = await PdfFindSectionTool().execute({"path": "test.pdf", "query": "chapter", "limit": limit}, {})
    assert result["exit_code"] == 0
    assert calls == [("test.pdf", "chapter", {"limit": limit})]


@pytest.mark.parametrize("limit", [1, 100, 20000])
async def test_valid_read_limit_reaches_backend_unchanged(monkeypatch, limit):
    calls = []

    def read(path, node_id, **kwargs):
        calls.append((path, node_id, kwargs))
        return {"id": "1", "title": "Chapter", "start_page": 1, "end_page": 2, "text": "text", "truncated": False}

    monkeypatch.setattr(pdf_tree, "read_section", read)
    result = await PdfReadSectionTool().execute({"path": "test.pdf", "node_id": "1", "max_chars": limit}, {})
    assert result["exit_code"] == 0
    assert calls == [("test.pdf", "1", {"max_chars": limit})]
