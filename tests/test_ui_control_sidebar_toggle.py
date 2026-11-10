"""The agent can put the app's sidebar away and bring it back.

`toggle sidebar off` hides it and `toggle sidebar on` shows it; the screen
applies the event (studio/src/screens/Studio.tsx). The everyday words for it
(nav, navigation, menu) mean the same switch.
"""
import asyncio

import pytest

from src.ai_interaction import do_ui_control


def test_toggle_sidebar_off_hides_it():
    r = asyncio.run(do_ui_control("toggle sidebar off"))
    assert r.get("ui_event") == "toggle"
    assert r.get("toggle_name") == "sidebar"
    assert r.get("state") is False
    assert "hidden" in r.get("results", "").lower()
    assert "error" not in r


def test_toggle_sidebar_on_shows_it():
    r = asyncio.run(do_ui_control("toggle sidebar on"))
    assert r.get("toggle_name") == "sidebar"
    assert r.get("state") is True
    assert "error" not in r


@pytest.mark.parametrize("word", ["nav", "navigation", "menu", "side_bar"])
def test_everyday_words_mean_the_sidebar(word):
    r = asyncio.run(do_ui_control(f"toggle {word} off"))
    assert r.get("toggle_name") == "sidebar" and r.get("state") is False


def test_the_other_toggles_keep_their_wording():
    r = asyncio.run(do_ui_control("toggle web on"))
    assert r.get("results") == "Toggle 'web' set to on"
