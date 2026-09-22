# -*- coding: utf-8 -*-
""""auto" is the sentinel everything passes, and nothing implemented it.

Found by giving the design canvas an agent tool. The tool came back in two
seconds with:

    design_canvas: the model call failed: Model 'auto' not found on any
    configured endpoint

`_resolve_model` was looking for a model literally NAMED "auto". Every caller
that has no opinion passes that string -- the chat route, `auto_review`,
`doubt_review`, `research_review`, the tournament, the tool-execution model
path. The review passes fail open, so they had been quietly not running; the
canvas does not fail open, which is how it finally surfaced.

An empty spec was worse than useless rather than merely broken: the Anthropic
branch matches with `model_name.lower() in am.lower()`, and "" is in every
string, so "" resolved to whichever model happened to be first.
"""

import pytest

from src.ai_interaction import _resolve_model


@pytest.mark.parametrize("spec", ["auto", "AUTO", " auto ", "", "   ", None])
def test_the_sentinel_resolves_to_the_configured_default(spec, monkeypatch):
    def _fake_get_setting(key, default=None):
        if key == "default_model":
            return "the-default-model"
        return default

    monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

    # No endpoint serves "the-default-model" here, so resolution fails -- and
    # the failure is the assertion: the name it went looking for must be the
    # default, never the literal "auto" and never the empty string.
    with pytest.raises(Exception) as excinfo:
        _resolve_model(spec)
    message = str(excinfo.value)
    assert "'auto'" not in message
    assert "''" not in message
    if "Model '" in message:
        assert "the-default-model" in message


def test_without_a_default_the_error_says_so(monkeypatch):
    def _fake_get_setting(key, default=None):
        if key == "default_model":
            return ""
        return default

    monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

    with pytest.raises(ValueError) as excinfo:
        _resolve_model("auto")
    message = str(excinfo.value)
    assert "no default model is configured" in message
    assert "Settings" in message


def test_an_explicit_model_is_left_alone(monkeypatch):
    """The sentinel must not swallow a real request."""
    def _fake_get_setting(key, default=None):
        if key == "default_model":
            return "the-default-model"
        return default

    monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

    with pytest.raises(Exception) as excinfo:
        _resolve_model("a-model-that-does-not-exist")
    assert "the-default-model" not in str(excinfo.value)
