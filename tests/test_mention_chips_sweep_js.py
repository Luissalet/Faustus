"""The @-chip sweep must not re-scan the whole transcript on every stream tick.

initMentionChips() attached an anonymous MutationObserver — no reference, so it
could never be disconnected — with subtree:true on #chat-history, and its
callback ran sweep() straight away. sweep() does a querySelectorAll over the
entire transcript, and the streaming renderer rewrites innerHTML several times a
second, so a long chat paid that scan on every tick.

decorate() made it worse: `if (!_workspace()) return 0;` sits *before*
`bodyEl.dataset.mentionChips = '1'`, so with no workspace bound — the default —
no message is ever marked and every one of them re-ran _workspace(), i.e. a
synchronous localStorage.getItem + JSON.parse, on every sweep.

Note the guard order in decorate() is deliberate and must stay: marking a body
as decorated while no workspace is bound would leave it chip-less forever once
one is. The fix is memoizing the workspace and bailing out of the sweep.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js" / "mentionChips.js"
_SRC = _JS.read_text(encoding="utf-8")
_WORKSPACE_JS = (_REPO / "static" / "js" / "workspace.js").read_text(encoding="utf-8")


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


def _nocomments(s):
    """Drop // comments so index comparisons measure code, not prose."""
    return re.sub(r"//[^\n]*", "", s)


def _ws_listener():
    """The addEventListener() wiring for the workspace-change event."""
    i = _SRC.index("addEventListener('odysseus:workspace-change'")
    return _SRC[i:i + 400]


# ── the observer ───────────────────────────────────────────────────────────

def test_the_observer_is_held_so_it_can_be_disconnected():
    assert "new MutationObserver(() => sweep())" not in _SRC, (
        "an anonymous observer can never be disconnected; it outlives every "
        "teardown"
    )
    assert re.search(r"\w+\s*=\s*new MutationObserver\(", _SRC), (
        "keep a reference to the observer"
    )
    assert ".disconnect()" in _SRC


def test_the_sweep_is_coalesced_instead_of_running_per_mutation():
    observe = re.search(r"new MutationObserver\(\s*(\w+)\s*\)", _SRC)
    assert observe, "observer callback moved; re-point this test"
    callback = observe.group(1)
    assert callback != "sweep", (
        "running sweep() straight off the observer means one whole-transcript "
        "querySelectorAll per mutation burst, several times a second while "
        "streaming"
    )
    scheduler = _nocomments(_body(_SRC, f"const {callback} = () =>"))
    assert "requestAnimationFrame" in scheduler, (
        "coalesce the burst into one sweep per frame"
    )
    assert re.search(r"if \(\w*[Pp]ending\)\s*return|if \(\w+\)\s*return", scheduler), (
        "the scheduler must drop repeat calls while a sweep is already queued"
    )


def test_the_sweep_bails_out_when_no_workspace_is_bound():
    body = _nocomments(_body(_SRC, "const sweep = () =>"))
    guard = body.index("_workspace()")
    scan = body.index("querySelectorAll")
    assert guard < scan, (
        "with no workspace bound no chip can be made, so skip the "
        "whole-transcript scan instead of re-deciding inside every message"
    )
    assert re.search(r"if \(!_workspace\(\)\)\s*return", body)


# ── the memoized workspace ─────────────────────────────────────────────────

def test_the_workspace_is_not_reparsed_on_every_call():
    body = _body(_SRC, "function _workspace()")
    assert "localStorage" not in body, (
        "_workspace() ran localStorage.getItem + JSON.parse synchronously on "
        "every call, once per message per sweep"
    )
    assert re.search(r"_wsCache|_cached|cache", body), "no memo at all"


def test_the_memo_is_invalidated_when_the_workspace_changes():
    assert "odysseus:workspace-change" in _WORKSPACE_JS, (
        "workspace.js must announce the change; nothing else can tell, since "
        "switching workspaces never reloads the page"
    )
    dispatch = _body(_WORKSPACE_JS, "export function setWorkspace(")
    assert "odysseus:workspace-change" in dispatch, (
        "the event has to fire from setWorkspace(), the single place the bound "
        "folder is written"
    )
    assert "addEventListener('odysseus:workspace-change'" in _SRC, (
        "mentionChips must listen, or its memo goes stale the moment the user "
        "switches project"
    )
    assert re.search(r"invalidateWorkspace\(\)|_wsCache\s*=\s*null", _ws_listener())


def test_binding_a_workspace_still_decorates_the_older_messages():
    # decorate() must keep skipping (not marking) bodies while no workspace is
    # bound, so they can be decorated once one is.
    body = _nocomments(_body(_SRC, "export function decorate("))
    ws_guard = body.index("_workspace()")
    mark = body.index("dataset.mentionChips = '1'")
    assert ws_guard < mark, (
        "marking a body as decorated with no workspace bound would leave it "
        "chip-less forever afterwards"
    )
    assert re.search(r"[Ss]weep\(\)", _ws_listener()), (
        "binding a workspace has to trigger a sweep, or the messages already "
        "on screen stay plain until the next mutation"
    )
