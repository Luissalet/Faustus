"""The composer's auto-resize measuring clone must never widen the chat
container. Seen live (ronda 6, Windows/Chrome): the clone is `position:
absolute` without left/top, so it kept its static position AFTER the real
textarea — a full composer width to the right — and a scrollIntoView()
scrolled the (overflow-x: hidden) chat container horizontally: the whole
chat drifted ~300 px to the left until reload."""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_resize_clone_is_pinned_and_anonymous():
    src = _read("static/js/ui.js")
    body = src[src.index("export function autoResize("):]
    body = body[:body.index("\n}\n")]
    assert "clone.style.left = '0'" in body and "clone.style.top = '0'" in body
    # The clone is a measuring device, not a second composer.
    for attr in ("id", "aria-label", "autofocus", "required"):
        assert f"clone.removeAttribute('{attr}')" in body, attr
    assert "clone.setAttribute('aria-hidden', 'true')" in body


def test_chat_container_clips_horizontal_overflow():
    css = _read("static/style.css")
    m = re.search(r"\.chat-container \{(.*?)\}", css, re.S)
    assert m and "overflow-x: clip" in m.group(1)
