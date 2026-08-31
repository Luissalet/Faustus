"""Guardrails for the three traps in static/style.css (FAUSTUS).

`style.css` is ~42k lines and opens with element-type rules that quietly
outrank the class rules of anything added later. Each of these cost real
debugging time on the Projects panel, so each becomes a test with a frozen
baseline: existing offenders are grandfathered, new ones fail.

  1. `button { height: 32px }` — a new button component that must grow with its
     content has to say `height: auto`; `min-height` does not undo it.
  2. `button:hover { background-color: var(--panel) }` — (0,1,1) beats a
     component's base `.x { background: … }` (0,1,0), so any button class that
     paints itself must also define its own `:hover`, or it flips to the panel
     colour under the cursor (white-on-white in the light theme).
  3. `-webkit-line-clamp` no longer clamps in current Chrome — `display:
     -webkit-box` computes as `flow-root` — so line clamping silently stops
     working and text overflows its box. Use `max-height: Nlh` instead.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="ignore")
CSS = re.sub(r"/\*.*?\*/", "", RAW, flags=re.S)
# @keyframes blocks use `from`/`to`/`50%` as block headers; they are not
# selectors and would read as global type rules.
_NO_KEYFRAMES = re.sub(
    r"@(-webkit-)?keyframes[^{]*\{(?:[^{}]*\{[^{}]*\}\s*)*\}", "", CSS, flags=re.S)
RULES = re.findall(r"([^{}]+)\{([^{}]*)\}", _NO_KEYFRAMES)

# ── frozen baselines: what was already there when the guardrails went in ──
KNOWN_TYPE_SELECTORS = {"a", "body", "button", "code", "details", "html",
                        "input", "pre", "select", "summary", "textarea"}
KNOWN_UNHOVERED_BUTTONS = {
    "attach-crop-btn", "cmp-header-action-btn", "cmp-rm-btn", "color-reset-btn",
    "cookbook-server-rm-btn", "cookbook-stop-btn", "copy-btn", "email-menu-btn",
    "gallery-fav-btn", "hwfit-engine-btn", "hwfit-usecase-btn",
    "mobile-new-chat-btn", "note-form-type-btn", "scroll-nav-btn",
    "shortcut-action-btn", "stall-banner-btn", "stop-btn",
    "vision-editor-btn-primary",
}
MAX_LINE_CLAMP_USES = 4


def selectors():
    for sel, body in RULES:
        for part in sel.split(","):
            part = part.strip()
            if part:
                yield part, body


def test_no_new_global_type_selectors():
    """Every bare `button {}`/`input {}` rule is a trap for future components."""
    found = {sel for sel, _ in selectors() if re.fullmatch(r"[a-z][a-z0-9]*", sel)}
    assert found <= KNOWN_TYPE_SELECTORS, (
        f"new global type selectors: {sorted(found - KNOWN_TYPE_SELECTORS)}. "
        "Scope them to a class instead — they outrank every class rule added later.")


def test_button_classes_that_paint_themselves_define_their_own_hover():
    painted, hovered = set(), set()
    for sel, body in selectors():
        base = re.fullmatch(r"\.([a-zA-Z0-9_-]+)", sel)
        if base and re.search(r"(^|;|\s)background(-color)?\s*:", body):
            name = base.group(1)
            if "btn" in name or "button" in name:
                painted.add(name)
        hov = re.match(r"\.([a-zA-Z0-9_-]+):hover", sel)
        if hov:
            hovered.add(hov.group(1))
    missing = painted - hovered - KNOWN_UNHOVERED_BUTTONS
    assert not missing, (
        f"button classes with their own background but no own :hover: {sorted(missing)}. "
        "The global `button:hover` will repaint them — add `.x:hover` (or "
        "`.x:hover:not(:disabled)`) declaring the background explicitly.")


def test_line_clamp_is_not_spreading():
    uses = len(re.findall(r"-webkit-line-clamp", CSS))
    assert uses <= MAX_LINE_CLAMP_USES, (
        f"{uses} uses of -webkit-line-clamp (baseline {MAX_LINE_CLAMP_USES}). "
        "It no longer clamps in current Chrome — use `max-height: Nlh` with "
        "`overflow: hidden`.")


def test_the_faustus_block_practises_what_it_preaches():
    """The service-health panel is the newest component in the file."""
    # Comments are stripped above, so anchor on the first selector of the block.
    block = CSS[CSS.index(".svc-health-chip"):]
    assert "height: auto" in block
    assert ":hover:not(:disabled)" in block


@pytest.mark.parametrize("baseline,label", [
    (KNOWN_UNHOVERED_BUTTONS, "unhovered buttons"),
    (KNOWN_TYPE_SELECTORS, "type selectors"),
])
def test_baselines_only_shrink(baseline, label):
    """A baseline is a debt list. If you fixed one, delete it from the set —
    this test tells you when the list is stale rather than letting it rot."""
    if label == "unhovered buttons":
        hovered = {m.group(1) for m in
                   (re.match(r"\.([a-zA-Z0-9_-]+):hover", sel) for sel, _ in selectors())
                   if m}
        stale = baseline & hovered
        assert not stale, f"fixed, remove from the baseline: {sorted(stale)}"
    else:
        found = {sel for sel, _ in selectors() if re.fullmatch(r"[a-z][a-z0-9]*", sel)}
        stale = baseline - found
        assert not stale, f"gone, remove from the baseline: {sorted(stale)}"
