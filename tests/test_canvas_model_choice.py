# -*- coding: utf-8 -*-
"""Which model writes the canvas.

Seen live: the canvas call went to the small helper model, spent 179.8 s
under the schema grammar and returned an empty completion. It got there by
resolving its own "auto" against the global default. A canvas is a
seven-field object and the grammar and the reasoning come out of one token
budget, so the smallest model on the box is the worst choice for it.
"""

from src.agent_tools.design_canvas_tools import _canvas_model


def _setting(value):
    def _get(key, default=None):
        if key == "agent_design_canvas_model":
            return value
        return default
    return _get


def test_the_turn_model_is_used_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", _setting(""))
    assert _canvas_model({"turn_model": "the-27b"}) == "the-27b"


def test_auto_in_the_setting_is_not_a_choice(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", _setting("auto"))
    assert _canvas_model({"turn_model": "the-27b"}) == "the-27b"


def test_an_explicit_setting_wins_over_the_turn_model(monkeypatch):
    """Somebody who picked a canvas model meant it."""
    monkeypatch.setattr("src.settings.get_setting", _setting("a-chosen-model"))
    assert _canvas_model({"turn_model": "the-27b"}) == "a-chosen-model"


def test_no_setting_and_no_turn_model_leaves_it_to_the_pass(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", _setting(""))
    assert _canvas_model({}) is None


def test_a_broken_settings_store_does_not_take_the_tool_down(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("src.settings.get_setting", _boom)
    assert _canvas_model({"turn_model": "the-27b"}) == "the-27b"
