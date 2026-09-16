"""Agents tab: every worker card must be reachable by scrolling.

The board (`.fs-sa-board`) lives inside `.fs-panel__body`, a column flex
with `overflow: auto`. `overflow: hidden` on a flex item zeroes its
automatic min-size, so the board shrinks to the tab and clips later
workers (agent 3 of 3 in the screenshot) instead of scrolling.

The board must be a scroll container (`overflow: auto` / `overflow-y: auto`)
so shrinking still leaves a way to reach every card.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CSS = _REPO / "studio" / "src" / "screens" / "studio.css"


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"missing {selector} in studio.css"
    return re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)


def _scrolls(block: str) -> bool:
    return bool(re.search(r"overflow(?:-y)?\s*:\s*auto\b", block))


def test_panel_body_is_the_tab_scroll_container():
    css = _CSS.read_text(encoding="utf-8")
    body = _rule(css, ".fs-panel__body")
    assert "overflow: auto" in body
    assert "min-block-size: 0" in body
    assert "flex-direction: column" in body


def test_subagent_board_scrolls_when_it_outgrows_the_agents_tab():
    css = _CSS.read_text(encoding="utf-8")
    board = _rule(css, ".fs-sa-board")
    cards = _rule(css, ".fs-sa-board__cards")
    assert _scrolls(board) or _scrolls(cards), (
        ".fs-sa-board (or .fs-sa-board__cards) must overflow:auto so a tall "
        "list of workers can scroll inside the Agents tab; overflow:hidden "
        "clips them with no scrollbar"
    )
    assert not re.search(r"overflow\s*:\s*hidden\b", board) or _scrolls(cards), (
        "overflow:hidden on .fs-sa-board without a scrolling cards region "
        "clips workers that do not fit the panel"
    )
