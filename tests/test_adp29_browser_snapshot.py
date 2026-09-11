"""ADP-29 — ask for a subtree or a search instead of the whole snapshot.

`src/browser_view.py` already had `parse_snapshot_elements`/`element_present`
(WEB-04: exact ref identity, tolerant of the two real `@playwright/mcp`
textual shapes — see `tests/test_l64_web04_snapshot_parser.py`). This adds
two PURE functions built on the same parser:

* `subtree(snapshot, ref, depth)` — the lines rooted at one element, bounded
  by indentation depth, never a substring guess at where it ends;
* `search(snapshot, query, limit)` — `{ref, role, name}` hits by role/name
  substring, capped, and strictly smaller than the full dump.

Both reuse `FORMAT_A`/`FORMAT_B` from the WEB-04 test so "the same element,
found either way" is provable rather than assumed.
"""
import json

from src.browser_view import search, subtree

FORMAT_A = """- generic [ref=e1]:
  - banner [ref=e2]:
    - link "Home" [ref=e3]
    - link "Products" [ref=e4]
  - main [ref=e5]:
    - heading "Welcome back" [level=1] [ref=e6]
    - button "Delete item" [ref=e7] [cursor=pointer]
    - textbox "Search" [ref=e8]
"""

FORMAT_B = """1. banner ref=e2
2. link: Home (ref=e3)
3. link: Products (ref=e4)
4. heading: Welcome back (ref=e6)
5. button: Delete item (ref=e7)
6. textbox: Search (ref=e8)
"""


# ── subtree ────────────────────────────────────────────────────────────

def test_subtree_depth_zero_is_only_the_matched_line():
    result = subtree(FORMAT_A, "e2", depth=0)
    assert result.strip() == '- banner [ref=e2]:'


def test_subtree_depth_one_includes_direct_children_not_grandchildren():
    result = subtree(FORMAT_A, "e1", depth=1)
    assert "ref=e2" in result and "ref=e5" in result
    assert "ref=e3" not in result and "ref=e7" not in result


def test_subtree_stops_at_a_sibling_not_the_whole_rest_of_the_page():
    """`e2`'s (banner's) subtree must not swallow `main`'s subtree just
    because it comes later in the same snapshot."""
    result = subtree(FORMAT_A, "e2", depth=5)
    assert "ref=e3" in result and "ref=e4" in result
    assert "ref=e5" not in result and "ref=e7" not in result


def test_subtree_of_a_leaf_is_just_the_leaf():
    result = subtree(FORMAT_A, "e7", depth=5)
    assert result.strip() == '- button "Delete item" [ref=e7] [cursor=pointer]'


def test_subtree_is_smaller_than_the_full_dump_with_the_same_element_findable():
    result = subtree(FORMAT_A, "e1", depth=1)
    assert len(result) < len(FORMAT_A)
    # e5 (a direct child kept at depth=1) is still findable in the trimmed text.
    assert "ref=e5" in result


def test_subtree_unknown_ref_is_empty_not_the_whole_page():
    assert subtree(FORMAT_A, "e404", depth=3) == ""


def test_subtree_of_no_ref_or_empty_snapshot_is_empty():
    assert subtree(FORMAT_A, "", depth=3) == ""
    assert subtree("", "e1", depth=3) == ""
    assert subtree(None, "e1", depth=3) == ""


def test_subtree_on_flat_relay_format_degrades_to_just_the_matched_line():
    """FORMAT_B carries no indentation to derive children from — the same
    "no crash, no guess" rule the WEB-04 parser already follows."""
    result = subtree(FORMAT_B, "e3", depth=5)
    assert result.strip() == "2. link: Home (ref=e3)"


def test_negative_depth_behaves_like_zero_not_unbounded():
    assert subtree(FORMAT_A, "e1", depth=-5) == subtree(FORMAT_A, "e1", depth=0)


# ── search ─────────────────────────────────────────────────────────────

def test_search_matches_by_role_or_name_case_insensitively():
    hits = search(FORMAT_A, "button")
    assert [h["ref"] for h in hits] == ["e7"]

    hits = search(FORMAT_A, "home")
    assert [h["ref"] for h in hits] == ["e3"]


def test_search_returns_less_text_than_the_full_dump_with_the_same_element_found():
    """The ADP-29 acceptance criterion, almost verbatim: search comes back
    with fewer characters than the whole snapshot, and the element it finds
    is the same one `parse_snapshot_elements` would find in the full text."""
    from src.browser_view import parse_snapshot_elements

    hits = search(FORMAT_A, "delete")
    serialized = json.dumps(hits)
    assert len(serialized) < len(FORMAT_A)
    full = {e["ref"]: e for e in parse_snapshot_elements(FORMAT_A)}
    assert hits[0] == full["e7"]


def test_search_is_capped_by_limit():
    haystack = "\n".join(f'- link "item" [ref=e{i}]' for i in range(50))
    hits = search(haystack, "item", limit=5)
    assert len(hits) == 5


def test_search_both_snapshot_formats_agree_on_the_same_page():
    a = {h["ref"] for h in search(FORMAT_A, "link")}
    b = {h["ref"] for h in search(FORMAT_B, "link")}
    assert a == b == {"e3", "e4"}


def test_search_empty_query_or_snapshot_returns_nothing():
    assert search(FORMAT_A, "") == []
    assert search(FORMAT_A, "   ") == []
    assert search("", "button") == []
    assert search(None, "button") == []


def test_search_no_match_is_an_empty_list_not_an_error():
    assert search(FORMAT_A, "nonexistent-widget-xyz") == []
