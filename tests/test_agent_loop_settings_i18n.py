"""Every setting of the advisor, typed decisions, declared risk and the stuck
watch is on the Agent settings screen, and its label and help have a Spanish
entry (the screen translates schema text through the i18n dictionary)."""
import json
import re
from pathlib import Path

import pytest

from src.agent_settings_schema import build_schema
from src.settings import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parent.parent
KEYS = [
    "advisor_enabled", "advisor_model", "advisor_max_uses", "advisor_max_tokens",
    "advisor_context_tokens", "mode_effort_advisor",
    "typed_decision_error_fork", "typed_decision_tool_tie", "typed_decision_tool_tie_tolerance",
    "typed_decision_compaction_keep", "self_declared_risk",
    "agent_loop_breaker_monologue_rounds", "agent_loop_breaker_context_error_limit",
    "agent_loop_breaker_failed_path_limit",
]


def _fields():
    return {f["key"]: f for g in build_schema()["groups"] for f in g["fields"]}


def _spanish_keys():
    text = (ROOT / "studio" / "src" / "i18n" / "es.ts").read_text(encoding="utf-8")
    return {json.loads(m.group(1)) for m in re.finditer(r'^  ("(?:[^"\\]|\\.)*"):', text, re.M)}


@pytest.mark.parametrize("key", KEYS)
def test_setting_is_declared_and_shown(key):
    assert key in DEFAULT_SETTINGS
    assert key in _fields(), f"{key} is not on the Agent settings screen"


@pytest.mark.parametrize("key", KEYS)
def test_label_and_help_have_a_spanish_entry(key):
    field = _fields()[key]
    spanish = _spanish_keys()
    assert field["label"] in spanish, f"{key}: no Spanish label for {field['label']!r}"
    assert field["help"] in spanish, f"{key}: no Spanish help"


def test_the_screen_translates_schema_text():
    src = (ROOT / "studio" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
    assert "{t(f.label)}" in src and "{t(f.help)}" in src and "{t(g.title)}" in src
