"""A refused truncate must stop the edit, not be ignored.

Editing a message or regenerating from one means asking the server to drop
everything after it, and only then sending again. When that request failed —
500, 404, a 409 from a chat that moved on — the caller carried on anyway: the
bubbles disappeared from the screen and the message was re-sent, while the
server still held the old tail. The transcript on screen and the transcript
on disk then disagreed, and a reload "undid" work the person had watched
happen.

The rule is one line long and easy to lose in a refactor: on failure, say so
and return, before anything is removed and before anything is sent.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """The callback assigned to `name`, up to its closing dependency array."""
    start = _SRC.index(f"const {name} = useCallback(")
    end = _SRC.index("  );", start)
    return _SRC[start:end]


def test_the_truncate_is_awaited_and_its_failure_returns():
    body = _body("regenerateFrom")
    assert "await truncateSession(" in body, "the truncate must be awaited, not fired and forgotten"

    catch = body.index("catch")
    ret = body.index("return;", catch)
    assert ret - catch < 220, "the catch must return, not fall through to the send"


def test_nothing_is_removed_or_sent_before_the_server_agrees():
    """The order is the whole guard: truncate, then trim, then send."""
    body = _body("regenerateFrom")
    truncate = body.index("await truncateSession(")
    trim = body.index("setTurns(")
    send = body.index("void run(")
    assert truncate < trim < send, (
        "the transcript must not be trimmed, and the message must not be re-sent, "
        "until the server has actually dropped the tail"
    )


def test_the_failure_is_told_to_the_person():
    """Silently doing nothing looks like a broken button."""
    body = _body("regenerateFrom")
    catch = body[body.index("catch"):]
    assert "say(" in catch, "a refused truncate has to be said out loud"
    assert re.search(r"'danger'", catch), "and said as a failure, not as a note"


def test_editing_and_regenerating_share_the_one_guarded_path():
    """Two callers, one rule. The bug was three copies of it, one wrong."""
    edit = _body("onEdit") if "const onEdit = useCallback(" in _SRC else ""
    assert "regenerateFrom(" in edit, (
        "editing must go through the same guarded truncate as regenerating, "
        "not truncate on its own"
    )


def test_every_truncate_is_inside_a_try():
    """`/truncate` types the same request by hand. It needs the same guard:
    a refusal must not leave the screen showing a chat the server still has
    in full."""
    for match in re.finditer(r"await truncateSession\(", _SRC):
        before = _SRC[max(0, match.start() - 400):match.start()]
        assert "try {" in before, (
            "a truncate call outside a try: a refusal would go unnoticed"
        )
        after = _SRC[match.end():match.end() + 700]
        assert "catch" in after, "and its failure has to be caught"
