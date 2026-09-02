"""selectSession's history render must not wipe a turn that is streaming in
the same chat. Seen (ronda 6, e2e under load — deterministic once the tool
index started building on the first request): the user hit send while the
chat was still opening; the history render that landed afterwards did
`chatHistory.innerHTML = ''`, the bubbles vanished and the answer streamed
into a detached node. Source-level pin of the guard (the flow itself is
exercised by tests/e2e/test_agent_flows.py)."""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "static" / "js" / "sessions.js").read_text(encoding="utf-8")


def _select_session_body():
    i = _SRC.index("export async function selectSession(")
    return _SRC[i:i + 30000]


def test_a_stream_in_flight_for_the_chat_blocks_the_wipe():
    body = _select_session_body()
    guard = body.index("streamingHere = !!(window.chatModule && window.chatModule.hasActiveStream")
    would_wipe = body.index("const wouldWipe = ")
    first_wipe = body.index("chatHistory.innerHTML = '';")
    assert guard < would_wipe < first_wipe, "the guard must run before the first innerHTML wipe"
    m = re.search(r"const wouldWipe = (.+);", body)
    assert "streamingHere" in m.group(1) and "hasExistingBubbles" in m.group(1)


def test_the_guard_clears_the_opening_placeholder_and_the_welcome_screen():
    body = _select_session_body()
    seg = body[body.index("if (wouldWipe) {"):body.index("if (wouldWipe) {") + 700]
    assert ".session-loading-state" in seg and "hideWelcomeScreen" in seg
    assert "return;" in seg
