"""Lote 49b (P1) - A11Y-03: `prefers-contrast: more` support in
studio/src/styles/base.css — density and reduced-motion were already
covered (shell.css's `data-density`, the reduced-motion blocks across
styles/*); high contrast had no support anywhere before this lot.

Static source check on the real deployed stylesheet.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_CSS = (REPO_ROOT / "studio" / "src" / "styles" / "base.css").read_text(encoding="utf-8")


def test_prefers_contrast_more_is_honoured():
    assert "@media (prefers-contrast: more)" in BASE_CSS


def test_high_contrast_widens_the_focus_ring_using_existing_tokens_only():
    block = BASE_CSS.split("@media (prefers-contrast: more)", 1)[1]
    block = block[: block.index("\n}\n") + 3] if "\n}\n" in block else block
    assert "outline-width" in block
    # Every colour must still come from tokens.css, never a hardcoded value —
    # the DESIGN.md rule this file's own header states for the whole tree.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "high-contrast block must not hardcode a colour"
    assert "var(--fs-border" in block


def test_no_outline_is_ever_removed_by_the_new_block():
    block = BASE_CSS.split("@media (prefers-contrast: more)", 1)[1]
    assert "outline: none" not in block
    assert "outline:none" not in block
