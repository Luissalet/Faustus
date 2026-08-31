"""Two agentHarnessUI.js regressions: a dead Cancel guard and a frozen panel.

1. "↻ Re-run…" asked for a model with window.prompt(). Cancel returns null, but
   `(window.prompt(...) || '').trim()` turned it into '' *before*
   `if (model === null) return;`, so that guard could never fire: Cancel and
   "OK with an empty field" were indistinguishable and both re-delegated the
   worker — another generation on the GPU, and its files rewritten.

2. restoreProgress() returned the _lastTodosBySession entry and never asked the
   server. chat.js drops progress_update events for background chats
   (`if (_isBg) continue;`), so that cache holds whatever was on screen when the
   user left: start a task in A showing "1/5", go to B, A finishes, come back to
   A — still "1/5". The Map also grew without bound.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js" / "agentHarnessUI.js"
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


def _strip_line_comments(s):
    return re.sub(r"//[^\n]*", "", s)


# ── Cancel on the re-run prompt ────────────────────────────────────────────

def test_cancel_is_checked_before_the_value_is_normalized():
    body = _body(_SRC, "function _rerunWorker(")
    prompt = body.index("window.prompt(")
    null_check = re.search(r"===\s*null", body)
    assert null_check, "_rerunWorker() no longer distinguishes Cancel at all"
    trim = body.index(".trim()")
    assert prompt < null_check.start() < trim, (
        "the null check must sit between window.prompt() and the .trim() that "
        "normalizes it — otherwise Cancel has already become '' and the guard "
        "is dead code"
    )


def test_cancel_never_reaches_the_delegation():
    body = _body(_SRC, "function _rerunWorker(")
    assert "|| '').trim()" not in body, (
        "`(window.prompt(...) || '').trim()` swallows the null that Cancel "
        "returns"
    )
    null_check = re.search(r"===\s*null", body)
    delegate = body.index("delegateTasks(")
    assert null_check.start() < delegate, (
        "Cancel must return before delegateTasks() starts another worker run"
    )


# ── the Progress panel on the way back into a chat ─────────────────────────

def test_restore_progress_always_refetches_from_the_server():
    body = _body(_SRC, "export async function restoreProgress(")
    cache_read = body.index("_lastTodosBySession.get(sessionId)")
    fetch_call = body.index("/api/agent/progress/")
    between = _strip_line_comments(body[cache_read:fetch_call])
    assert not re.search(r"\breturn\b", between), (
        "returning on a cache hit is what freezes the panel: progress_update "
        "events are dropped for background chats, so the cached entry is stale "
        "exactly when the user comes back"
    )
    assert "renderProgress(cached" in body, (
        "still paint the cache first so the panel does not flash empty"
    )
    assert fetch_call > cache_read


def test_the_server_answer_wins_over_the_cache():
    body = _body(_SRC, "export async function restoreProgress(")
    fetch_call = body.index("/api/agent/progress/")
    assert "renderProgress(data.todos" in body[fetch_call:], (
        "the fetched todos must be painted, otherwise the refetch changes nothing"
    )
    assert "_currentSessionId !== sessionId" in body[fetch_call:], (
        "a late answer for a chat the user already left must not be painted"
    )


def test_the_todo_cache_is_bounded():
    assert re.search(r"_PROGRESS_CACHE_MAX|CACHE_MAX|MAX_CACHE", _SRC), (
        "_lastTodosBySession grew one entry per chat opened, for the life of "
        "the tab"
    )
    assert "_lastTodosBySession.delete(" in _SRC, (
        "a cap needs an eviction; nothing ever removed an entry"
    )
