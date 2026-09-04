"""Static guards for the Studio tree (UI-012).

The old interface carries measured debt - 92 `transition: all`, 110+
`outline: none` without a replacement, interactive divs, icons whose only
name is a `title`. None of it is fixed here: each screen clears its own
area as it migrates. What these guards do is stop the debt reproducing
inside the clean layer, which is the failure mode that makes a rewrite
pointless.

They are deliberately static and dependency-free: a rule that needs a
toolchain to run is a rule that stops running.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parents[1] / "studio" / "src"

# tokens.css is where literal values are ALLOWED to live - it is the file
# that defines them. legacy-bridge.css is the second: every literal there
# is a var() fallback for a theme variable that may not be set, which is
# the one place a raw colour is the correct answer.
TOKEN_FILES = {"tokens.css", "legacy-bridge.css"}

# A guard with no way out gets deleted the first time it is inconvenient.
# `guard-ok:` exempts a single line and must carry its reason on that line,
# so the exemption is reviewed where it lives instead of in a config file.
EXEMPTION = "guard-ok:"


def _sources(*suffixes: str) -> list[Path]:
    if not STUDIO.exists():  # the Studio tree is optional until UI-002 lands
        return []
    return [p for p in STUDIO.rglob("*") if p.suffix in suffixes and p.is_file()]


def _rel(path: Path) -> str:
    return path.relative_to(STUDIO.parent.parent).as_posix()


def _offences(pattern: str, suffixes: tuple[str, ...], skip: set[str] = frozenset()) -> list[str]:
    rx = re.compile(pattern)
    found: list[str] = []
    for path in _sources(*suffixes):
        if path.name in skip:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line) and EXEMPTION not in line:
                found.append(f"{_rel(path)}:{number}: {line.strip()}")
    return found


def test_no_transition_all() -> None:
    """`transition: all` animates layout too, and cannot be reasoned about."""
    offences = _offences(r"transition\s*:\s*all", (".css", ".tsx", ".ts"))
    assert not offences, "transition: all is forbidden in Studio:\n" + "\n".join(offences)


def test_no_outline_none() -> None:
    """Removing the outline costs keyboard users their position on the page."""
    offences = _offences(r"outline\s*:\s*none", (".css",))
    assert not offences, "outline: none without replacement:\n" + "\n".join(offences)


def test_no_interactive_div() -> None:
    """A div with onClick is invisible to the keyboard and to a screen reader."""
    offences = _offences(r"<(div|span)[^>]*\sonClick", (".tsx",))
    assert not offences, "use a <button>, not a clickable div:\n" + "\n".join(offences)


def test_no_inline_svg() -> None:
    """index.html already holds 262 of these. The new layer uses lucide-react."""
    offences = _offences(r"<svg[\s>]", (".tsx",))
    assert not offences, "no inline SVG in Studio; import from lucide-react:\n" + "\n".join(offences)


def test_colours_come_from_tokens() -> None:
    """Every literal colour outside tokens.css is a token that was not created."""
    offences = _offences(
        r"(#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\()",
        (".css", ".tsx"),
        skip=TOKEN_FILES,
    )
    # rgb(0 0 0 / x) in the overlay backdrop is the one legitimate literal:
    # a scrim is not a surface and has no token.
    offences = [o for o in offences if "fs-overlay-backdrop" not in o and "rgb(0 0 0 / 0.5)" not in o]
    assert not offences, "hardcoded colour; add a token instead:\n" + "\n".join(offences)


def test_durations_come_from_tokens() -> None:
    """Three durations exist. A fourth one invented inline is drift."""
    offences = _offences(r"(transition|animation)[^;]*\b\d+ms", (".css",), skip=TOKEN_FILES)
    # Keyframe-driven loops declare their own period; they are named, not ad hoc.
    offences = [o for o in offences if "fs-spin" not in o and "fs-shimmer" not in o and "fs-breathe" not in o]
    assert not offences, "hardcoded duration; use --fs-duration-*:\n" + "\n".join(offences)


def test_reduced_motion_is_handled() -> None:
    """Any file that animates must say what happens when motion is reduced."""
    for path in _sources(".css"):
        text = path.read_text(encoding="utf-8")
        if "animation:" in text and "@keyframes" in text:
            assert "prefers-reduced-motion" in text, (
                f"{_rel(path)} animates but never handles prefers-reduced-motion"
            )


def test_studio_tree_exists() -> None:
    """Fail loudly if the guards are silently scanning nothing."""
    assert _sources(".tsx"), "no Studio sources found - are the guards pointed at the right tree?"
