"""artifact_search hands the model readable snippets, not JSON escapes."""
from src.agent_tools.artifact_read_tool import render_hits
from src.tool_execution import format_tool_result


def test_snippets_keep_their_line_breaks():
    hits = [{"artifact_id": "art_1", "tool": "bash", "start": 10, "end": 90,
             "snippet": "line one\nERROR: disk full\nline three", "score": -1.2}]
    text = render_hits("disk full", hits)
    assert "ERROR: disk full\nline three" in text
    assert "\\n" not in text
    assert "art_1" in text and "10-90" in text and "read_artifact" in text
    shown = format_tool_result("artifact_search", {"hits": hits, "count": 1, "output": text})
    assert "ERROR: disk full\nline three" in shown


def test_no_hits_says_so():
    assert "no stored result" in render_hits("x", [])
