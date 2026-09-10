"""L20 (integrates L13): the four autonomy-budget ceilings that used to be
fixed module constants in ``src/autonomy_budget.py`` (``max_active_seconds``,
``max_subagents``, ``max_remote_spend``, ``max_memory_mb``) must be visible
and changeable like every other agent ceiling: registered in
``DEFAULT_SETTINGS``, described in ``agent_settings_schema`` and actually
read by ``resolve_budget`` through ``get_setting`` — not just carried as
constants nobody can see or override on a running install.
"""
from __future__ import annotations

from src import agent_settings_schema as schema_mod
from src import autonomy_budget as ab
from src.settings import DEFAULT_SETTINGS

_NEW_KEYS = (
    "agent_autonomy_max_active_seconds",
    "agent_autonomy_max_subagents",
    "agent_autonomy_max_remote_spend",
    "agent_autonomy_max_memory_mb",
)


def _field(key):
    for group in schema_mod.GROUPS:
        for f in group["fields"]:
            if f["key"] == key:
                return group["id"], f
    raise AssertionError(f"{key} is not in the settings schema")


def test_all_four_ceilings_are_registered_defaults():
    for key in _NEW_KEYS:
        assert key in DEFAULT_SETTINGS, f"{key} missing from DEFAULT_SETTINGS"
        assert isinstance(DEFAULT_SETTINGS[key], int)


def test_all_four_ceilings_are_in_the_schema_as_numeric_fields():
    for key in _NEW_KEYS:
        _group, field = _field(key)
        assert field["type"] in ("int", "float")
        assert field["min"] == 0  # 0 = unlimited, the shared convention


def test_schema_parity_check_has_no_complaints():
    """tests/test_agent_settings_schema.py enforces this globally; pinned
    here too so this file alone proves the new keys did not slip through."""
    problems = schema_mod.schema_problems()
    assert problems == [], problems


def test_resolve_budget_now_scales_the_four_settings_backed_ceilings():
    settings = {
        "agent_autonomy_max_active_seconds": 100,
        "agent_autonomy_max_subagents": 2,
        "agent_autonomy_max_remote_spend": 50,
        "agent_autonomy_max_memory_mb": 10,
    }
    get_setting = lambda key, default=None: settings.get(key, default)
    supervised = ab.resolve_budget("supervised", get_setting=get_setting)
    bounded = ab.resolve_budget("bounded_autonomous", get_setting=get_setting)
    read_only = ab.resolve_budget("read_only", get_setting=get_setting)

    # Directly reflects the configured base (multiplier 1.0 for supervised),
    # not the module's fixed fallback (900 / 4 / 20_000 / 2048).
    assert supervised.max_active_seconds == 100.0
    assert supervised.max_subagents == 2
    assert supervised.max_remote_spend == 50.0
    assert supervised.max_memory_mb == 10.0

    assert bounded.max_active_seconds > supervised.max_active_seconds
    assert read_only.max_active_seconds < supervised.max_active_seconds
    assert bounded.max_subagents > supervised.max_subagents
    assert bounded.max_remote_spend > supervised.max_remote_spend
    assert bounded.max_memory_mb > supervised.max_memory_mb


def test_resolve_budget_ignores_the_unlimited_sentinel_for_the_four_new_ceilings():
    """0 means unlimited to the rest of the codebase; a preset must not
    become unlimited on this dimension just because the setting was left at
    its 0 default (same rule already proven for max_tool_calls/max_tokens)."""
    get_setting = lambda key, default=None: 0
    budget = ab.resolve_budget("supervised", get_setting=get_setting)
    assert budget.max_active_seconds and budget.max_active_seconds > 0
    assert budget.max_subagents and budget.max_subagents > 0
    assert budget.max_remote_spend and budget.max_remote_spend > 0
    assert budget.max_memory_mb and budget.max_memory_mb > 0
