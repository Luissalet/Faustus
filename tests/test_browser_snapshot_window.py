"""New-element marks, windows and page_find over the stored browser snapshot."""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from src import browser_snapshot_window as bsw
from src import browser_actions, browser_view
from src import mcp_manager as mm
from src.agent_tools.page_snapshot_tools import PageFindTool, PageWindowTool

HEADER = "### Page\n- Page URL: https://shop.example/list\n- Page Title: Shop\n### Snapshot\n"


def _snap(items, nav=True, extra=""):
    lines = []
    if nav:
        lines += ['- banner [ref=e1]:', '  - navigation [ref=e2]:',
                  '    - link "Home" [ref=e3]', '    - link "Cart" [ref=e4]']
    lines += [f'- listitem [ref=e{10 + i}]:\n  - link "{name}" [ref=e{100 + i}]' for i, name in enumerate(items)]
    return HEADER + "\n".join(lines) + extra


@pytest.fixture(autouse=True)
def _clean():
    bsw.reset()
    yield
    bsw.reset()


# ── (a) new-element marks ───────────────────────────────────────────────────

def test_first_snapshot_of_a_page_has_no_marks():
    out, handled = bsw.process_snapshot("s", _snap(["A", "B"]), limit=12000, mark_new=True, paging=False)
    assert not handled
    assert "- * " not in out
    assert "marks elements new" not in out


def test_second_snapshot_marks_only_new_elements():
    bsw.process_snapshot("s", _snap(["A", "B"]), limit=12000, mark_new=True, paging=False)
    out, _ = bsw.process_snapshot("s", _snap(["A", "B", "C"]), limit=12000, mark_new=True, paging=False)
    marked = [ln for ln in out.splitlines() if "- * " in ln]
    assert any('link "C"' in ln for ln in marked)
    assert not any('link "A"' in ln or 'link "B"' in ln for ln in marked)
    assert "new since the previous snapshot of this page: 2" in out   # the listitem and its link


def test_same_ref_with_a_new_name_counts_as_new():
    bsw.process_snapshot("s", _snap(["A"]), limit=12000, mark_new=True, paging=False)
    out, _ = bsw.process_snapshot("s", _snap(["A renamed"]), limit=12000, mark_new=True, paging=False)
    assert '- * link "A renamed"' in out


def test_other_page_and_other_connection_do_not_share_state():
    bsw.process_snapshot("s", _snap(["A"]), limit=12000, mark_new=True, paging=False)
    other_page = _snap(["A", "B"]).replace("/list", "/other")
    out, _ = bsw.process_snapshot("s", other_page, limit=12000, mark_new=True, paging=False)
    assert "- * " not in out
    out2, _ = bsw.process_snapshot("t", _snap(["A", "B"]), limit=12000, mark_new=True, paging=False)
    assert "- * " not in out2


def test_marks_off_leaves_text_identical():
    text = _snap(["A", "B"])
    bsw.process_snapshot("s", text, limit=12000, mark_new=False, paging=False)
    out, handled = bsw.process_snapshot("s", _snap(["A", "B", "C"]), limit=12000, mark_new=False, paging=False)
    assert out == _snap(["A", "B", "C"]) and not handled


def test_marked_snapshot_still_parses_as_elements():
    bsw.process_snapshot("s", _snap(["A"]), limit=12000, mark_new=True, paging=False)
    out, _ = bsw.process_snapshot("s", _snap(["A", "B"]), limit=12000, mark_new=True, paging=False)
    refs = {e["ref"]: e for e in browser_view.parse_snapshot_elements(out)}
    assert refs["e101"]["role"] == "link" and refs["e101"]["name"] == "B"
    assert browser_view.element_present("e101", out)


def test_numbered_list_shape_is_marked_and_parsed():
    a = "### Page\n- Page URL: https://x.example/\n1. button: Save (ref=e1)"
    b = a + "\n2. button: Delete (ref=e2)"
    bsw.process_snapshot("s", a, limit=12000, mark_new=True, paging=False)
    out, _ = bsw.process_snapshot("s", b, limit=12000, mark_new=True, paging=False)
    assert "2. * button: Delete (ref=e2)" in out
    assert [e["name"] for e in browser_view.parse_snapshot_elements(out)] == ["Save", "Delete"]


# ── (c) windows ─────────────────────────────────────────────────────────────

def _big(n=400):
    return _snap([f"Product number {i} with a long descriptive title" for i in range(n)])


def test_paging_serves_a_window_with_nav_and_next_hint():
    text = _big()
    out, handled = bsw.process_snapshot("s", text, limit=2000, mark_new=False, paging=True)
    assert handled
    body = out.split("\n[window:")[0]
    assert len(body) <= 2000 + 20          # the window itself stays within the budget
    assert out.startswith("### Page")
    # the first window already contains the navigation, so it is not repeated there
    assert out.count('link "Cart" [ref=e4]') == 1
    footer = out.splitlines()[-1]
    assert footer.startswith("[window: chars 0-") and 'page_window {"offset": ' in footer
    assert "page_find" in footer
    snap = bsw.latest(["s"])
    second = bsw.render_window(snap, 2000, 2000)
    assert "--- page navigation (repeated in every window) ---" in second
    assert 'link "Cart" [ref=e4]' in second.split("--- page navigation")[1]


def test_windows_cover_the_whole_snapshot_without_gaps():
    text = _big(120)
    out, _ = bsw.process_snapshot("s", text, limit=1500, mark_new=False, paging=True)
    snap = bsw.latest(["s"])
    seen = []
    offset = 0
    for _ in range(200):
        body, start, end = bsw.cut_window(snap.shown, offset, 1500)
        seen.append(body)
        if end >= len(snap.shown):
            break
        offset = end
    assert "\n".join(seen) == snap.shown.rstrip("\n")


def test_next_window_repeats_nav_and_reports_last_window():
    _big_text = _big(60)
    bsw.process_snapshot("s", _big_text, limit=1500, mark_new=False, paging=True)
    snap = bsw.latest(["s"])
    out = bsw.render_window(snap, len(snap.shown) - 400, 1500)
    assert "--- page navigation" in out
    assert "This is the last window." in out
    assert out.startswith("### Page window of https://shop.example/list")


def test_small_snapshot_is_not_windowed():
    out, handled = bsw.process_snapshot("s", _snap(["A"]), limit=12000, mark_new=False, paging=True)
    assert not handled and "[window" not in out


def test_one_giant_line_still_advances():
    body, start, end = bsw.cut_window("x" * 5000 + "\nshort", 0, 1000)
    assert end == 5001 and body.endswith("[line clipped]")


def test_nav_block_is_bounded_and_falls_back_to_links():
    many = "\n".join(f'    - link "Item {i}" [ref=e{i}]' for i in range(200))
    text = "- navigation [ref=e1]:\n" + many
    nav = bsw.nav_block(text, 300)
    assert len(nav) <= 300 + 260
    assert nav.startswith("- navigation")
    no_landmark = "\n".join(f'- link "L{i}" [ref=e{i}]' for i in range(30))
    fallback = bsw.nav_block(no_landmark, 600)
    assert fallback.count("link") <= 12 and fallback


def test_precondition_finds_element_in_another_window():
    text = _big(300)
    out, handled = bsw.process_snapshot("s", text, limit=1500, mark_new=False, paging=True)
    assert handled and 'ref=e399' not in out
    pre = browser_actions.ActionPrecondition(require_element="e399")
    assert browser_actions.check_precondition(pre, current_url="https://shop.example/list", snapshot_text=out) is None
    missing = browser_actions.ActionPrecondition(require_element="e9999")
    assert browser_actions.check_precondition(missing, current_url="", snapshot_text=out)


# ── (b) page_find ───────────────────────────────────────────────────────────

def _run(tool, args, ctx=None):
    return asyncio.run(tool.execute(json.dumps(args), ctx or {}))


def test_page_find_returns_matches_with_refs_and_context():
    bsw.process_snapshot("builtin_browser", _big(50), limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), {"query": "number 7 with", "context": 1})
    assert res["exit_code"] == 0
    assert res["total_matches"] == 1
    m = res["matches"][0]
    assert m["ref"] == "e107" and m["before"] and m["after"] == [] or m["after"] is not None
    assert "[ref=e107]" in res["output"]
    assert len(res["output"]) < 1500      # not the page


def test_page_find_regex_case_and_limits():
    bsw.process_snapshot("builtin_browser", _big(50), limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), {"query": r"Product number 4\d ", "regex": True, "max_matches": 3})
    assert res["total_matches"] == 10 and len(res["matches"]) == 3
    assert "showing the first 3" in res["output"]
    none = _run(PageFindTool(), {"query": "PRODUCT NUMBER 7 ", "case_sensitive": True})
    assert none["total_matches"] == 0 and "Nothing matched" in none["output"]


def test_page_find_reports_nearest_ref_for_text_lines():
    text = HEADER + '- article [ref=e5]:\n  - paragraph: Free shipping over 50 euros'
    bsw.process_snapshot("builtin_browser", text, limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), {"query": "free shipping"})
    assert res["matches"][0]["under_ref"] == "e5"


@pytest.mark.parametrize("args, msg", [
    ({"query": ""}, "required"),
    ({"query": "("  , "regex": True}, "valid regular expression"),
    ({"query": "a*", "regex": True}, "empty string"),
    ({"query": "x" * 400}, "longer than"),
])
def test_page_find_rejects_bad_queries(args, msg):
    bsw.process_snapshot("builtin_browser", _snap(["A"]), limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), args)
    assert res["exit_code"] == 1 and msg in res["error"]


def test_page_find_without_snapshot_and_stale_id():
    assert "no browser snapshot" in _run(PageFindTool(), {"query": "a"})["error"]
    bsw.process_snapshot("builtin_browser", _snap(["A"]), limit=500, mark_new=False, paging=False)
    stale = _run(PageFindTool(), {"query": "A", "snapshot_id": "deadbeef"})
    assert "not the latest" in stale["error"]


def test_page_find_never_reads_another_sessions_browser(monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    mine = session_browser_server_id("alice", "s1")
    theirs = session_browser_server_id("bob", "s2")
    bsw.process_snapshot(theirs, HEADER + '- link "Bob secret" [ref=e1]', limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), {"query": "secret"}, {"owner": "alice", "session_id": "s1"})
    assert res["exit_code"] == 1
    bsw.process_snapshot(mine, HEADER + '- link "Alice page" [ref=e1]', limit=500, mark_new=False, paging=False)
    res = _run(PageFindTool(), {"query": "Alice"}, {"owner": "alice", "session_id": "s1"})
    assert res["total_matches"] == 1


def test_page_window_tool_walks_the_snapshot():
    text = _big(200)
    bsw.process_snapshot("builtin_browser", text, limit=1500, mark_new=False, paging=True)
    first = bsw.latest(["builtin_browser"])
    res = _run(PageWindowTool(), {"offset": 1500, "limit": 1500, "snapshot_id": first.snapshot_id})
    assert res["exit_code"] == 0
    assert "### Page window of" in res["output"]
    assert "--- page navigation" in res["output"]
    bad = _run(PageWindowTool(), {"offset": 10 ** 9})
    assert bad["exit_code"] == 1
    stale = _run(PageWindowTool(), {"offset": 0, "snapshot_id": "00000000"})
    assert "not the latest" in stale["error"]


# ── integration with the MCP manager post-processing ────────────────────────

def test_postprocess_uses_settings_and_keeps_errors_verbatim(monkeypatch):
    values = {"browser_snapshot_max_chars": 1500, "browser_snapshot_mark_new": True,
              "browser_snapshot_paging": True}
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: values.get(k, d))
    big = _big(300)
    res = mm.McpManager._postprocess_browser_result(
        "browser_snapshot", {"exit_code": 0, "stdout": big}, server_id="builtin_browser")
    assert "[window: chars 0-" in res["stdout"] and "truncated" not in res["stdout"]
    err = {"exit_code": 1, "stdout": big, "error": "boom"}
    assert mm.McpManager._postprocess_browser_result("browser_snapshot", err, server_id="x") is err


def test_postprocess_default_settings_keep_truncation(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda k, d=None: 1000 if k == "browser_snapshot_max_chars" else False)
    big = _big(300)
    res = mm.McpManager._postprocess_browser_result(
        "browser_snapshot", {"exit_code": 0, "stdout": big}, server_id="builtin_browser")
    assert "snapshot truncated to 1000 chars" in res["stdout"]
    assert "page_find" in res["stdout"]
    # ...but the full page stays reachable through page_find
    found = _run(PageFindTool(), {"query": "number 299 "})
    assert found["total_matches"] == 1


def test_defaults_are_off_and_declared():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["browser_snapshot_mark_new"] is False
    assert DEFAULT_SETTINGS["browser_snapshot_paging"] is False
