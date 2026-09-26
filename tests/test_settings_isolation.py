"""tests/conftest.py's `isolated_settings_file` autouse fixture.

Before this fixture, nothing in conftest.py isolated `src.settings.
SETTINGS_FILE`, so any test reaching the real `save_settings` -- directly,
or through a helper like `src.safe_mode.quarantine` -- wrote into the
checkout's actual `data/settings.json`. A stray `disabled_tools` entry left
there then broke `tests/test_browser_mcp_reconnect.py`'s fake-server tests
depending on run order (see that fixture's docstring). This file proves the
isolation holds without depending on any one "culprit" test.
"""
from __future__ import annotations

import os

from src import constants
from src import settings as settings_mod


def test_settings_file_is_repointed_away_from_the_real_checkout_path():
    real_path = os.path.join(constants.DATA_DIR, "settings.json")
    assert settings_mod.SETTINGS_FILE != real_path
    assert not os.path.exists(real_path), (
        "a prior test already wrote the real settings.json -- isolation regressed"
    )


def test_a_real_save_settings_call_never_touches_the_real_checkout_file():
    """The exact write shape that caused the regression: something calls the
    real `save_settings` with `disabled_tools` in it (safe_mode quarantine,
    extension_manifest, the manage_settings tool, ...). Wherever it comes
    from, it must land in this test's isolated file, never the real one."""
    real_path = os.path.join(constants.DATA_DIR, "settings.json")

    settings_mod.save_settings({"disabled_tools": ["some-server"]})

    assert not os.path.exists(real_path)
    assert os.path.exists(settings_mod.SETTINGS_FILE)
    assert settings_mod.get_setting("disabled_tools", []) == ["some-server"]


def test_each_test_gets_its_own_settings_file(tmp_path):
    """Two tests writing conflicting settings must never see each other's
    data -- the isolation is per-test, not a single shared tmp file."""
    assert settings_mod.get_setting("disabled_tools", []) == []
    settings_mod.save_settings({"disabled_tools": ["only-this-test"]})
    assert settings_mod.get_setting("disabled_tools", []) == ["only-this-test"]
