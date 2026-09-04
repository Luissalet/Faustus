"""Two settings that already worked, now visible in Settings.

`agent_subagent_depth` and `agent_fixer_resume` were read through
`get_setting(key, default)` from the day they shipped, so they were live —
but they were not in `DEFAULT_SETTINGS` and not in the schema, which meant the
Settings page never drew a control for either. A ceiling on how many
generations of workers one turn may produce is exactly the kind of limit a
human should be able to see before they need it, and an operator who does not
know a fix round resumes the previous worker cannot reason about the
transcript it produces.

Registering them must not MOVE either default: this file pins that the readers
answer the same thing they answered before, through the registered value.
"""

from __future__ import annotations

import pytest

from src import agent_settings_schema as schema_mod
from src.settings import DEFAULT_SETTINGS


def _field(key):
    for group in schema_mod.GROUPS:
        for f in group["fields"]:
            if f["key"] == key:
                return group["id"], f
    raise AssertionError(f"{key} is not in the settings schema")


def test_the_delegation_ceiling_is_registered_where_a_human_will_look():
    group, field = _field("agent_subagent_depth")
    assert group == "subagents"
    assert field["type"] == "int" and (field["min"], field["max"]) == (0, 4)
    assert DEFAULT_SETTINGS["agent_subagent_depth"] == 1


def test_the_fixer_resume_toggle_is_registered():
    group, field = _field("agent_fixer_resume")
    assert group == "reliability"
    assert field["type"] == "bool"
    assert DEFAULT_SETTINGS["agent_fixer_resume"] is True


def test_registering_did_not_move_either_default():
    """The two modules that read these keys have their own hardcoded
    fallbacks. Registering a different number would give one machine a
    different ceiling from another depending on whether a settings file had
    ever been written."""
    from src import subagent_permissions

    assert subagent_permissions.DEFAULT_MAX_DEPTH == DEFAULT_SETTINGS["agent_subagent_depth"]


@pytest.mark.parametrize("key,stored,expected", [
    # The schema is also what `POST /api/auth/settings` coerces through, so a
    # ceiling typed into the form cannot land outside its own bounds.
    ("agent_subagent_depth", "3", 3),
    ("agent_subagent_depth", 99, 4),
    ("agent_subagent_depth", -1, 0),
    ("agent_fixer_resume", "false", False),
    ("agent_fixer_resume", "true", True),
])
def test_the_form_coerces_and_clamps(key, stored, expected):
    assert schema_mod.coerce_setting_value(key, stored) == expected


def test_the_readers_answer_the_registered_default(monkeypatch):
    from src import dispatch, subagent_permissions

    # A machine with no settings file: `load_settings` merges DEFAULT_SETTINGS,
    # so what the reader sees is the value this file just pinned.
    monkeypatch.setattr("src.settings.load_settings", lambda: dict(DEFAULT_SETTINGS))
    assert subagent_permissions.max_depth() == 1
    assert dispatch.resume_enabled() is True

    monkeypatch.setattr("src.settings.load_settings",
                        lambda: dict(DEFAULT_SETTINGS, agent_subagent_depth=0, agent_fixer_resume=False))
    assert subagent_permissions.max_depth() == 0
    assert dispatch.resume_enabled() is False
