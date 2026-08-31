"""A failed /truncate must stop the edit / resend / regenerate, not be ignored.

_truncateWithVersion() returned `null` both when the server refused (500 / 404 /
409) and when it trimmed the chat but had no version worth offering an Undo for.
Its three callers could not tell those apart, so on a refusal they still deleted
the bubbles from #chat-history and re-sent the message. The server-side history
was untouched, so the model received the original message *and* the edited one,
the "deleted" messages came back on the next reload, and nothing on screen said
a word about it.

The contract pinned here: the function reports failure, and none of the three
callers touches the DOM or re-sends before checking the result.
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = _REPO / "static" / "js" / "chat.js"
_SRC = _CHAT_JS.read_text(encoding="utf-8")

# What each caller does *after* the truncate that it must not do when the
# server kept the messages: delete the transcript, or re-send.
_DESTRUCTIVE = {
    "editUserMessage": ("allMsgs[i].remove();", "submitBtn.click();"),
    "resendUserMessage": ("sibling.remove();", "_hideUserBubble = true;", "submitBtn.click();"),
    "regenerateFrom": ("allMsgs[i].remove();", "aiMsgElement.remove();", "submitBtn.click();"),
}


def _body(src, header):
    """The `header` declaration plus its brace-balanced body.

    The opening brace is looked for *after* the parameter list, so a default
    like `opts = {}` does not end the function on its first character.
    """
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


def _truncate_body():
    return _body(_SRC, "async function _truncateWithVersion(")


# ── the function itself ────────────────────────────────────────────────────

def test_a_non_ok_response_is_not_swallowed():
    body = _truncate_body()
    assert re.search(r"if\s*\(\s*!\s*r\.ok\s*\)", body), (
        "`if (r.ok) saved = ...` treats a 500 as 'nothing to undo' and falls "
        "through to the caller as a success"
    )
    assert not re.search(r"return\s+null\s*;", body), (
        "returning null for a failure is indistinguishable from 'trimmed, but "
        "no version to offer'"
    )


def test_failure_and_success_without_a_version_are_distinguishable():
    body = _truncate_body()
    assert re.search(r"ok\s*:\s*false", body), "no failure result"
    assert re.search(r"ok\s*:\s*true", body), "no success result"
    # The success-with-no-version case must still be a success.
    ok_true = body.index("ok: true") if "ok: true" in body else body.index("ok:true")
    assert "version" in body[ok_true:ok_true + 80]


def test_the_network_error_path_reports_failure_too():
    body = _truncate_body()
    catch = body[body.index("catch (err)"):]
    assert re.search(r"ok\s*:\s*false", catch), (
        "a thrown fetch (offline, aborted) must be reported as a failure"
    )


# ── the three callers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", sorted(_DESTRUCTIVE))
def test_the_caller_checks_the_result_before_touching_anything(fn):
    body = _body(_SRC, f"export async function {fn}(")
    tail = body[body.index("_truncateWithVersion("):]

    guard = re.search(r"if\s*\([^)]*\.ok[^)]*\)", tail)
    assert guard, f"{fn}() never looks at what _truncateWithVersion() returned"

    for marker in _DESTRUCTIVE[fn]:
        pos = tail.index(marker)
        assert guard.start() < pos, (
            f"{fn}(): {marker!r} runs before the truncate result is checked"
        )
        assert re.search(r"\breturn\s*;", tail[guard.end():pos]), (
            f"{fn}(): the failure branch must return before {marker!r}"
        )


@pytest.mark.parametrize("fn", sorted(_DESTRUCTIVE))
def test_the_caller_tells_the_user_it_aborted(fn):
    body = _body(_SRC, f"export async function {fn}(")
    tail = body[body.index("_truncateWithVersion("):]
    guard = re.search(r"if\s*\([^)]*\.ok[^)]*\)", tail)
    stop = tail.index("return;", guard.end())
    assert "showError" in tail[guard.end():stop], (
        f"{fn}() aborts silently — the user sees the edit/resend simply not "
        "happen, with no idea why. Use the file's existing showError()."
    )


def test_the_three_callers_are_the_only_ones():
    # A new caller must opt into the same guard; this fails loudly if one appears.
    calls = [m.start() for m in re.finditer(r"await _truncateWithVersion\(", _SRC)]
    assert len(calls) == 3, f"expected 3 truncate callers, found {len(calls)}"
