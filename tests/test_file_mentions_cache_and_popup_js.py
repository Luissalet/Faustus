"""Two fileMentions.js regressions: a workspace-blind cache and a ghost popup.

1. The lookup cache was keyed on `query.toLowerCase()` alone. The workspace goes
   into the request URL, but switching workspaces never reloads the page, so
   "@src" in project B replayed project A's file list — and picking a row
   inserted a path that does not exist in B.

2. blur neither bumped `seq` nor cancelled the debounce timer, so a slow lookup
   could call show() after the hide() and leave the popup floating; and there
   was no "click outside → hide" branch to close it either.

Source-level, like the other composer contracts here: initFileMentions() needs a
live textarea and a document, and what broke is the wiring, not a pure helper.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js" / "fileMentions.js"
_SRC = _JS.read_text(encoding="utf-8")


def _body(src, header):
    """The `header` declaration plus its brace-balanced body."""
    i = src.index(header)
    p = src.index("(", i)
    depth = 0
    for k in range(p, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                p = k
                break
    j = src.index("{", p)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _handler(event):
    """The body of the addEventListener(...) call for `event`."""
    i = _SRC.index(f"addEventListener('{event}'")
    return _SRC[i:_SRC.index("addEventListener", i + 20)] if _SRC.find(
        "addEventListener", i + 20) > 0 else _SRC[i:]


# ── the cache must know which workspace the answer came from ───────────────

def test_the_lookup_cache_is_keyed_by_workspace_too():
    body = _body(_SRC, "const load = async (workspace, query) =>")
    key = re.search(r"const key\s*=\s*([^;]+);", body)
    assert key, "the cache key moved; re-point this test"
    assert "workspace" in key.group(1), (
        "keying on the query alone serves project A's files for '@src' typed "
        "in project B — and inserts a path that does not exist there"
    )


def test_the_workspace_is_still_what_the_request_asks_for():
    # Guard against "fixing" the key by dropping the workspace from the URL.
    body = _body(_SRC, "const load = async (workspace, query) =>")
    assert "workspace=${encodeURIComponent(workspace)}" in body


# ── the popup must not outlive the token it was opened for ─────────────────

def test_hide_invalidates_the_lookup_in_flight():
    body = _body(_SRC, "const hide = () =>")
    assert re.search(r"\bseq\s*\+\+|\+\+\s*seq\b", body), (
        "without bumping the sequence, a slow response calls show() after the "
        "hide and the popup reappears on its own"
    )
    assert "clearTimeout(" in body


def test_blur_cancels_the_pending_lookup():
    h = _handler("blur")
    assert re.search(r"\bseq\s*\+\+|\+\+\s*seq\b|hide\(\)", h), (
        "blur only scheduled a delayed hide; a lookup already in flight still "
        "landed and re-showed the popup"
    )
    assert re.search(r"\bseq\s*\+\+|\+\+\s*seq\b", h), (
        "invalidate the in-flight lookup at blur time, not 120 ms later"
    )
    assert "clearTimeout(" in h


def test_a_click_outside_closes_the_popup():
    h = _handler("mousedown")
    assert "hide()" in h, (
        "the mousedown handler only inserted a clicked row; clicking anywhere "
        "else left the popup floating over the page"
    )
    # …and it must still insert when the click *is* on a row.
    insert_idx = h.index("insert(row.dataset.rel)")
    hide_idx = h.index("hide()")
    assert insert_idx < hide_idx, (
        "picking a row must take priority over the outside-click close"
    )
