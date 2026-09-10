"""Lote 64 — WEB-04: element resolution independent of the textual format
`@playwright/mcp` happens to emit.

Before this, `src/browser_actions.py::check_precondition` decided whether a
`ref` was "still on the page" with a bare substring test
(`ref in snapshot_text`) — format-agnostic by accident, but wrong whenever
one ref string is a substring of another (`"e1" in "...[ref=e10]..."` is
True). `browser_view.parse_snapshot_elements`/`element_present` parse the
snapshot into `{ref, role, name}` rows and match by exact ref identity, and
are tolerant of two real snapshot shapes:

  * Format A — the current aria-snapshot tree (dash-prefixed, bracketed
    trailing attributes, quoted name): ``- button "Delete item" [ref=e7]``
  * Format B — a flat/numbered relay shape some MCP bridges and older
    builds produce (colon-separated name, parenthesised ref):
    ``5. button: Delete item (ref=e7)``

Integration note (see the batch report's "Cambios necesarios en ficheros
ajenos"): `src/browser_actions.py::check_precondition` is not in this lot's
file list, so it still calls the raw substring check today — this backend
capability is ready for that one-line swap.
"""
from src.browser_view import element_present, parse_snapshot_elements

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


def test_format_a_tree_parses_ref_role_and_name():
    elements = parse_snapshot_elements(FORMAT_A)
    by_ref = {e["ref"]: e for e in elements}
    assert by_ref["e7"]["role"] == "button"
    assert by_ref["e7"]["name"] == "Delete item"
    assert by_ref["e8"]["role"] == "textbox"
    assert by_ref["e8"]["name"] == "Search"
    # A parent container with no quoted name still yields a row (role only).
    assert by_ref["e1"]["role"] == "generic"
    assert by_ref["e1"]["name"] == ""


def test_format_b_flat_relay_parses_the_same_refs():
    elements = parse_snapshot_elements(FORMAT_B)
    by_ref = {e["ref"]: e for e in elements}
    assert by_ref["e7"]["role"] == "button"
    assert by_ref["e7"]["name"] == "Delete item"
    assert by_ref["e8"]["role"] == "textbox"
    assert by_ref["e8"]["name"] == "Search"
    assert by_ref["e3"]["name"] == "Home"


def test_both_formats_agree_on_the_same_page():
    """The whole point: two DIFFERENT textual shapes of the SAME page state
    resolve to the same element identities — resolution must not depend on
    which shape the MCP server/bridge happened to emit."""
    a = {e["ref"]: (e["role"], e["name"]) for e in parse_snapshot_elements(FORMAT_A)}
    b = {e["ref"]: (e["role"], e["name"]) for e in parse_snapshot_elements(FORMAT_B)}
    shared = set(a) & set(b)
    assert shared == {"e2", "e3", "e4", "e6", "e7", "e8"}
    for ref in shared:
        assert a[ref] == b[ref], f"{ref} disagrees between formats: {a[ref]} vs {b[ref]}"


def test_element_present_true_for_a_ref_still_on_the_page():
    assert element_present("e7", FORMAT_A) is True
    assert element_present("e7", FORMAT_B) is True


def test_element_present_false_for_a_ref_no_longer_on_the_page():
    stale_ref = "e99"
    assert element_present(stale_ref, FORMAT_A) is False
    assert element_present(stale_ref, FORMAT_B) is False


def test_element_present_rejects_a_prefix_collision_a_substring_check_would_miss():
    """The concrete bug this replaces: `"e1" in text` is True whenever the
    page ALSO has an unrelated `ref=e10` (or e1-anything), which is exactly
    the "blind click on a stale reference" WEB-04's acceptance forbids."""
    snapshot_with_only_e10 = '- button "Something else" [ref=e10]\n'
    naive_substring_check = "e1" in snapshot_with_only_e10
    assert naive_substring_check is True, "sanity: the old check really would false-positive here"

    assert element_present("e1", snapshot_with_only_e10) is False


def test_element_present_falls_back_to_substring_for_an_unparseable_shape():
    """A third shape neither parser branch recognises (no `ref=`/`ref:`
    token anywhere) must not make every precondition fail outright — it
    degrades to the old behaviour rather than refusing every action."""
    mystery_text = "the page shows a widget identified as e7 somewhere in prose"
    assert parse_snapshot_elements(mystery_text) == []
    assert element_present("e7", mystery_text) is True
    assert element_present("e404", mystery_text) is False


def test_element_present_with_no_ref_requested_is_always_true():
    assert element_present("", FORMAT_A) is True
    assert element_present(None, FORMAT_A) is True


def test_a_line_with_no_ref_token_is_skipped_not_guessed():
    text = "Page URL: https://example.test\nPage Title: Example\n" + FORMAT_A
    elements = parse_snapshot_elements(text)
    # Only the ref-bearing lines from FORMAT_A became elements.
    assert len(elements) == 8
