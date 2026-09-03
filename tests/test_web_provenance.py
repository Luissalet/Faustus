"""Browser provenance: anchors that survive a round trip, drift that is
reported instead of accepted, comments that do not disturb what the model
reads, and a setting-off run that is byte-identical
(src/web_provenance.py + the seam in src/browser_view.py)."""

import re

import pytest

from src import browser_view
from src import web_provenance as wp
from src.tool_capabilities import BROWSER_MCP_PREFIX


PAGE = (
    "# Quarterly report\n"
    "\n"
    "Revenue was 1,240 million euros in 2025.\n"
    "\n"
    "The board approved a dividend of 0.42 per share.\n"
)
URL = "https://example.com/reports/q4"
WHEN = "2026-09-03T10:00:00+00:00"


def _annotated(text=PAGE, url=URL, blocks=None):
    return wp.annotate(text, url=url, fetched_at=WHEN, blocks=blocks)


# ---------------------------------------------------------------------------
# annotate → extract round trip
# ---------------------------------------------------------------------------


def test_annotate_then_extract_returns_every_block_with_its_range_and_hash():
    annotated = _annotated()
    records = wp.extract_provenance(annotated)

    assert [r["block"] for r in records] == [0, 1, 2]
    assert {r["url"] for r in records} == {URL}
    for record in records:
        chunk = PAGE[record["start"]:record["end"]]
        assert chunk.strip()
        assert record["sha256"] == wp.block_hash(chunk)
    # The ranges are into the FETCHED document, not the annotated string.
    assert PAGE[records[1]["start"]:records[1]["end"]].startswith("Revenue was")


def test_the_document_comment_names_the_url_and_the_fetch_time():
    header = wp.document_provenance(_annotated())
    assert header == {"url": URL, "fetched_at": WHEN, "blocks": 3}


def test_stripping_the_comments_gives_back_the_exact_fetched_bytes():
    annotated = _annotated()
    assert annotated != PAGE
    assert wp.strip_provenance(annotated) == PAGE


def test_the_comments_are_html_comments_and_leave_the_prose_untouched():
    annotated = _annotated()
    visible = re.sub(r"<!--.*?-->\n?", "", annotated, flags=re.DOTALL)
    assert visible == PAGE
    # Nothing of the anchor leaks into a line of prose.
    for line in annotated.splitlines():
        assert not (line.strip().startswith("<!--") and not line.strip().endswith("-->"))
    assert "sha256=" not in visible and "block=" not in visible


def test_caller_supplied_ranges_are_used_verbatim():
    annotated = _annotated(blocks=[(2, 19)])
    records = wp.extract_provenance(annotated)
    assert len(records) == 1
    assert (records[0]["start"], records[0]["end"]) == (2, 19)
    assert records[0]["sha256"] == wp.block_hash(PAGE[2:19])


def test_overlapping_and_impossible_ranges_are_dropped_not_merged():
    annotated = _annotated(blocks=[(0, 10), (5, 20), (-4, 0), ("x", 3), (30, 40)])
    ranges = [(r["start"], r["end"]) for r in wp.extract_provenance(annotated)]
    assert ranges == [(0, 10), (30, 40)]


def test_annotate_is_a_no_op_on_empty_or_blank_text():
    assert wp.annotate("", url=URL, fetched_at=WHEN) == ""
    assert wp.annotate("   \n\n", url=URL, fetched_at=WHEN) == "   \n\n"
    assert wp.extract_provenance(None) == []
    assert wp.document_provenance("no comments here") is None


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_an_unchanged_source_verifies_clean():
    report = wp.verify_block(_annotated(), PAGE)
    assert report["ok"] is True
    assert report["checked"] == 3 and report["matched"] == 3 and report["drifted"] == []
    assert {row["status"] for row in report["blocks"]} == {"match"}


def test_a_changed_block_is_reported_as_drifted_never_accepted():
    drifted_source = PAGE.replace("1,240", "9,999")
    report = wp.verify_block(_annotated(), drifted_source)

    assert report["ok"] is False
    assert report["drifted"] == [1]
    row = report["blocks"][1]
    assert row["status"] == "drifted"
    assert row["sha256"] != row["actual_sha256"]
    assert "not the text that was fetched" in row["why"]
    # The blocks that did not change still verify: drift is per block.
    assert [r["status"] for r in report["blocks"]] == ["match", "drifted", "match"]


def test_a_range_that_no_longer_exists_is_drift_not_a_match():
    report = wp.verify_block(_annotated(), PAGE[:20])
    assert report["ok"] is False
    assert 2 in report["drifted"]
    assert any("past the end of the source" in row["why"] for row in report["blocks"])


def test_verify_block_on_junk_reports_nothing_checked_instead_of_raising():
    assert wp.verify_block(None, None)["checked"] == 0
    assert wp.verify_block("no anchors here", PAGE)["ok"] is True
    assert wp.verify_block(_annotated(), "")["ok"] is False


# ---------------------------------------------------------------------------
# The browser seam
# ---------------------------------------------------------------------------

SNAPSHOT_TOOL = BROWSER_MCP_PREFIX + "browser_snapshot"
CONSOLE_TOOL = BROWSER_MCP_PREFIX + "browser_console_messages"

RESULT_TEXT = (
    "- Page URL: https://example.com/a\n"
    "- Page Title: Example\n"
    "\n"
    "First paragraph of the page.\n"
    "\n"
    "Second paragraph of the page.\n"
)


def _result(text=RESULT_TEXT, **extra):
    out = {"stdout": text, "exit_code": 0}
    out.update(extra)
    return out


def test_page_text_is_anchored_to_the_url_the_result_itself_reports():
    out = browser_view.annotate_page_text(SNAPSHOT_TOOL, _result(), {},
                                          fetched_at=WHEN)
    assert out is not None
    assert out["provenance"]["url"] == "https://example.com/a"
    assert out["provenance"]["blocks"] >= 2
    assert "pixels" in out["provenance"]["anchor"]
    records = wp.extract_provenance(out["stdout"])
    assert records and all(r["url"] == "https://example.com/a" for r in records)
    # And the anchors check out against the text that was actually fetched.
    assert wp.verify_block(out["stdout"], RESULT_TEXT)["ok"] is True


def test_the_annotated_result_is_a_copy_and_keeps_every_other_field():
    original = _result(exit_code=0, images=[{"data": "x", "mimeType": "image/jpeg"}])
    out = browser_view.annotate_page_text(SNAPSHOT_TOOL, original, {},
                                          fetched_at=WHEN)
    assert out is not original
    assert original["stdout"] == RESULT_TEXT      # untouched
    assert out["images"] == original["images"] and out["exit_code"] == 0


def test_with_the_setting_off_nothing_is_rebuilt_so_the_run_is_byte_identical():
    original = _result()
    assert browser_view.annotate_page_text(
        SNAPSHOT_TOOL, original, {"agent_web_provenance": False}) is None
    assert original["stdout"] == RESULT_TEXT


def test_the_setting_defaults_to_on_and_survives_unreadable_settings():
    assert wp.enabled({}) is True
    assert wp.enabled({"agent_web_provenance": None}) is True
    assert wp.enabled({"agent_web_provenance": False}) is False


@pytest.mark.parametrize("tool,result,why", [
    (CONSOLE_TOOL, _result(), "console output is not page content"),
    (SNAPSHOT_TOOL, _result(blocked=True), "a refused action never ran"),
    (SNAPSHOT_TOOL, _result(approval_required=True), "parked at the approval card"),
    (SNAPSHOT_TOOL, _result("no url line here\n\nand no anchor"), "no URL to anchor to"),
    (SNAPSHOT_TOOL, {"stdout": ""}, "no text at all"),
    (SNAPSHOT_TOOL, "not a dict", "not a result"),
    ("read_file", _result(), "not a browser tool"),
])
def test_the_seam_declines_rather_than_inventing_an_anchor(tool, result, why):
    assert browser_view.annotate_page_text(tool, result, {}) is None, why


def test_an_already_anchored_result_is_not_anchored_twice():
    once = browser_view.annotate_page_text(SNAPSHOT_TOOL, _result(), {},
                                           fetched_at=WHEN)
    assert browser_view.annotate_page_text(SNAPSHOT_TOOL, once, {}) is None


def test_the_seam_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("annotator on fire")

    monkeypatch.setattr(wp, "annotate", boom)
    assert browser_view.annotate_page_text(SNAPSHOT_TOOL, _result(), {}) is None


# ── the seam is actually wired into the loop ──────────────────────────────
# A provenance module nothing calls is documentation, not a feature. These pin
# the wiring itself, because the annotation is invisible in the UI (the anchors
# are HTML comments) and a silent disconnection would never be noticed.

def _loop_source():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "src/agent_loop.py").read_text(encoding="utf-8")


def test_the_agent_loop_annotates_browser_results_before_the_model_sees_them():
    src = _loop_source()
    assert "annotate_page_text as _annotate_page" in src, \
        "the loop must call the provenance seam"
    assert "_model_result = result" in src and "format_tool_result(desc, _model_result)" in src, \
        "the annotated copy, not the original, is what gets formatted for the model"


def test_only_the_model_copy_is_annotated():
    """The tool_event, the harness ledger and everything the USER sees must keep
    the original object: the anchors are for the model's own citations."""
    src = _loop_source()
    head = src.split("_model_result = result", 1)[0]
    assert "tool_events.append(tool_event)" in head, \
        "the UI event is built before the annotated copy exists, so it cannot carry anchors"


def test_the_wiring_can_never_cost_a_turn():
    src = _loop_source()
    block = src.split("_model_result = result", 1)[1].split("formatted =", 1)[0]
    assert "try:" in block and "except Exception" in block, \
        "a provenance failure must degrade to the untouched result, never raise"
