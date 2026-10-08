"""Round 2 of the constrained-choice layer: a reply that contradicts itself is
never a choice, and the two settings are registered where a human looks.

Reviewer repro (#63): when a llama-server ignores the grammar and answers
``{"choice":"product","answer":"comparison"}``, the old reader took the first
known key and returned ``product`` with no repair. Two different options in one
reply are ambiguous; that must hold for JSON fields and duplicate keys too.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from src import agent_settings_schema as schema_mod
from src import constrained_choice as cc
from src.settings import DEFAULT_SETTINGS

OPTIONS = ["product", "comparison"]
ROOT = Path(__file__).resolve().parent.parent

CONFLICTS = [
    '{"choice":"product","answer":"comparison"}',          # the reviewer's payload
    '{"answer":"comparison","choice":"product"}',          # order must not matter
    '{"choice":"product","choice":"comparison"}',          # duplicate key, different values
    '{"choice":"product","Answer":"comparison"}',          # key case
    '{"choice":"product","label":["comparison"]}',         # one-element list is an answer too
    '{"choice":"product","answer":"banana"}',              # the second field is not an option
    '```json\n{"choice":"product","value":"comparison"}\n```',
]

CONSISTENT = [
    ('{"choice":"product","answer":"product"}', "product"),
    ('{"choice":"product","choice":"product"}', "product"),
    ('{"choice":"Product","answer":"product."}', "product"),   # same label once folded
    ('{"choice":"comparison","reason":"cheaper than a product"}', "comparison"),  # prose is not a field
    ('{"answer":"comparison"}', "comparison"),
    ('"product"', "product"),
]


@pytest.mark.parametrize("raw", CONFLICTS)
def test_a_contradicting_json_reply_is_not_a_choice(raw):
    assert cc.match_option(raw, OPTIONS) is None


@pytest.mark.parametrize("raw,expected", CONSISTENT)
def test_a_consistent_json_reply_is_still_accepted(raw, expected):
    assert cc.match_option(raw, OPTIONS) == expected


@pytest.mark.parametrize("raw", CONFLICTS[:3])
def test_the_ollama_reader_refuses_a_contradicting_wrapper(raw):
    assert cc._ollama_value(raw) is None


def test_the_ollama_reader_keeps_the_consistent_cases():
    assert cc._ollama_value('{"choice":"a","choice":"a"}') == "a"
    assert cc._ollama_value('{"choice": "general"}') == "general"
    assert cc._ollama_value('"product"') == "product"


def _install(monkeypatch, first_raw, repair_raw):
    calls = {"constrained": 0, "free": 0}

    async def fake_llamacpp(*args, **kwargs):
        calls["constrained"] += 1
        att = cc._Attempt(cc.PATH_LLAMACPP)
        att.raw = first_raw  # a server that ignored the submitted grammar
        return att

    async def fake_generation(path, *args, **kwargs):
        calls["free"] += 1
        att = cc._Attempt(path)
        att.raw = repair_raw
        return att

    monkeypatch.setattr(cc, "_attempt_llamacpp", fake_llamacpp)
    monkeypatch.setattr(cc, "_attempt_generation", fake_generation)
    monkeypatch.setattr(cc, "_setting", lambda key: True if key == "constrained_choice_enabled" else "auto")
    return calls


def _choose(**kwargs):
    return asyncio.run(cc.choose_one(OPTIONS, "classify", url="http://stub/v1/chat/completions",
                                     model="stub", backend="llamacpp", **kwargs))


def test_a_contradicting_reply_triggers_the_repair_and_the_repair_wins(monkeypatch):
    calls = _install(monkeypatch, '{"choice":"product","answer":"comparison"}', "comparison")
    result = _choose()
    assert calls == {"constrained": 1, "free": 1}
    assert result.choice == "comparison" and result.repaired is True
    assert result.honoured is None and result.path == cc.PATH_FREE
    assert len(result.attempts) == 2 and result.attempts[0]["ok"] is False


def test_a_contradicting_reply_and_a_failed_repair_is_none(monkeypatch):
    calls = _install(monkeypatch, '{"choice":"product","answer":"comparison"}',
                     '{"choice":"comparison","answer":"product"}')
    result = _choose()
    assert calls == {"constrained": 1, "free": 1}
    assert result.choice is None and result.reason == "unparsed" and result.repaired is True


def test_a_contradicting_reply_without_repair_is_none_after_one_attempt(monkeypatch):
    calls = _install(monkeypatch, '{"choice":"product","answer":"comparison"}', "product")
    result = _choose(repair=False)
    assert calls == {"constrained": 1, "free": 0}
    assert result.choice is None and result.reason == "grammar_ignored"
    assert result.honoured is False and len(result.attempts) == 1


def test_a_consistent_reply_needs_no_repair(monkeypatch):
    calls = _install(monkeypatch, '{"choice":"product","answer":"product"}', "comparison")
    result = _choose()
    assert calls == {"constrained": 1, "free": 0}
    assert result.choice == "product" and result.repaired is False and result.honoured is False


# --- settings ------------------------------------------------------------


def test_the_defaults_are_registered_and_do_not_move():
    assert DEFAULT_SETTINGS["constrained_choice_enabled"] is True
    assert DEFAULT_SETTINGS["constrained_choice_backend"] == "auto"
    for key, value in cc.DEFAULTS.items():
        assert DEFAULT_SETTINGS[key] == value, f"{key}: the module default and DEFAULT_SETTINGS differ"


def _fields():
    return {f["key"]: f for g in schema_mod.build_schema()["groups"] for f in g["fields"]}


def test_both_settings_are_on_the_agent_settings_screen():
    fields = _fields()
    enabled, backend = fields["constrained_choice_enabled"], fields["constrained_choice_backend"]
    assert enabled["type"] == "bool"
    assert backend["type"] == "select"
    assert [o["value"] for o in backend["options"]] == ["auto", "llamacpp", "ollama", "free"]


def test_the_schema_has_no_problems_with_the_new_fields():
    assert schema_mod.schema_problems() == []


def _spanish_keys():
    text = (ROOT / "studio" / "src" / "i18n" / "es.ts").read_text(encoding="utf-8")
    return {json.loads(m.group(1)) for m in re.finditer(r'^  ("(?:[^"\\]|\\.)*"):', text, re.M)}


@pytest.mark.parametrize("key", ["constrained_choice_enabled", "constrained_choice_backend"])
def test_label_and_help_have_a_spanish_entry(key):
    field = _fields()[key]
    spanish = _spanish_keys()
    assert field["label"] in spanish, f"{key}: no Spanish label"
    assert field["help"] in spanish, f"{key}: no Spanish help"


def test_the_settings_reader_gives_the_registered_value(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: {"constrained_choice_enabled": False}.get(key, default))
    assert cc._setting("constrained_choice_enabled") is False
    assert cc._setting("constrained_choice_backend") == "auto"
