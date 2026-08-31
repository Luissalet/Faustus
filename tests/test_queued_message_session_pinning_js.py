"""A queued message belongs to the chat it was typed in.

Sending one while the agent is still answering parks it as a "Queued" bubble.
The drain then hands it to handleChatSubmit(), which resolves its destination
from sessionModule.getCurrentSessionId() — so if the user has moved to another
chat in the meantime (the first stream keeps running in the background), the
queued text is posted into *that* conversation instead. #chat-history is wiped
on a session switch, so the "Queued" bubble is gone too and there is not even a
visual clue.

Same block, second failure: `_queuedPromoteTimer` was one module-level variable,
so promoting a second queued message clearTimeout()'d the first one's retry —
and that item had already been spliced out of the array and off the DOM by
_removeQueuedRequest(), so it was simply lost. The retry also rescheduled itself
every 220 ms forever if the stream never went idle.

chat.js is browser-heavy (one module, dozens of imports, no DOM in CI), so this
pins the source-level contract the way test_resend_message_nondestructive.py
does.
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = _REPO / "static" / "js" / "chat.js"
_SRC = _CHAT_JS.read_text(encoding="utf-8")

# Anything that names the destination chat counts as "this send is addressed".
_SESSION_AWARE = re.compile(
    r"_isQueueItemHere|getCurrentSessionId\s*\(|_queueSessionId\s*\(|\.sessionId\b"
)


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


def _queue_region():
    start = _SRC.index("const _queuedAgentRequests = [];")
    end = _SRC.index("export async function handleChatSubmit(")
    return _SRC[start:end]


# ── BUG: queued message sent into whichever chat happens to be open ─────────

def test_queued_items_record_the_chat_they_were_typed_in():
    body = _body(_SRC, "function _queueAgentRequest(")
    assert re.search(r"sessionId\s*:", body), (
        "the queue item must remember its origin session; without it the drain "
        "has no way to tell chat A's message from chat B's"
    )


def test_the_send_helper_refuses_to_post_into_another_chat():
    body = _body(_SRC, "function _setComposerAndSend(")
    assert re.search(r"function _setComposerAndSend\(\s*\w+\s*,\s*\w+", body), (
        "_setComposerAndSend() must take the chat the message is addressed to"
    )
    guard = _SESSION_AWARE.search(body)
    assert guard, "_setComposerAndSend() never checks which chat is on screen"
    submit = body.index("handleChatSubmit(")
    assert guard.start() < submit, (
        "the destination has to be checked before the submit, not after"
    )
    # The deferred submit runs a tick later — the user can switch chats inside
    # that tick, so the check must also happen inside the timeout.
    deferred = body[body.index("setTimeout("):submit]
    assert _SESSION_AWARE.search(deferred), (
        "re-check the destination inside the deferred send: a session switch "
        "can land between setting the composer and submitting it"
    )


def test_the_drain_only_sends_what_belongs_to_the_chat_on_screen():
    body = _body(_SRC, "function _drainQueuedAgentRequests(")
    assert "_queuedAgentRequests[0]" not in body, (
        "draining the head of the queue blindly posts chat A's message into "
        "whatever chat is open when A's stream ends"
    )
    assert _SESSION_AWARE.search(body), (
        "the drain must pick a message addressed to the current session"
    )


def test_no_queued_send_goes_out_unaddressed():
    region = _queue_region()
    for call in ("_setComposerAndSend(item.message)", "_setComposerAndSend(next.message)"):
        assert call not in region, (
            f"{call} sends to the active chat, not to the one the message was "
            "typed in"
        )


def test_a_message_for_another_chat_stays_in_the_queue():
    region = _queue_region()
    assert "_requeueQueuedRequest(" in region, (
        "an item taken out of the queue that could not be sent must go back "
        "in, not be dropped on the floor"
    )
    body = _body(_SRC, "function _requeueQueuedRequest(")
    assert "_queuedAgentRequests.push(" in body


def test_the_queued_bubbles_are_repainted_after_a_session_switch():
    # sessions.js clears #chat-history on every switch, taking the bubble host
    # with it; without a repaint the queued message is invisible.
    assert "_repaintQueuedBubbles" in _SRC
    listener = _SRC[_SRC.index("export function init(apiBase) {"):]
    listener = listener[:listener.index("agentHarnessUI.init(")]
    assert "odysseus:session-switch" in listener, (
        "chat.js must follow the existing session-switch event to retarget "
        "and repaint the queue"
    )


# ── BUG: promoting two queued messages loses the first ─────────────────────

def test_promotion_timers_are_per_item():
    assert "_queuedPromoteTimer" not in _SRC, (
        "one module-level promote timer means the second promote clearTimeout()s "
        "the first one's retry — and that item is already out of the array and "
        "off the DOM, so it is lost silently"
    )
    body = _body(_SRC, "function _sendQueuedWhenIdle(")
    assert re.search(r"\bitem\.promoteTimer\b", body), (
        "each queued item needs its own retry timer"
    )


def test_the_promote_retry_is_bounded_and_gives_the_message_back():
    body = _body(_SRC, "function _sendQueuedWhenIdle(")
    assert re.search(r"tries|attempts", body), (
        "the 220 ms retry rescheduled itself forever when the stream wedged"
    )
    assert "_requeueQueuedRequest(" in body, (
        "once the retries run out the message must reappear in the visible "
        "queue instead of vanishing into a timer nobody will fire"
    )


def test_removing_an_item_cancels_its_own_timer():
    body = _body(_SRC, "function _removeQueuedRequest(")
    assert "clearTimeout(" in body and "promoteTimer" in body, (
        "splicing an item out must stop its retry, or the timer fires against "
        "an item that is no longer queued"
    )


@pytest.mark.parametrize("fn", [
    "_queueAgentRequest",
    "_setComposerAndSend",
    "_drainQueuedAgentRequests",
    "_promoteQueuedRequest",
    "_sendQueuedWhenIdle",
])
def test_the_queue_helpers_still_exist(fn):
    # Guard the extraction above: a rename should fail loudly here, not make
    # the assertions vacuous.
    assert f"function {fn}(" in _SRC
