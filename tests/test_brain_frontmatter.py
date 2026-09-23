"""Round-trip and shape tests for `src/brain/frontmatter.py`.

Determinism here is what lets `vault.sync` skip writing a file whose
rendered content did not change (Lot A's "sync twice writes nothing" bar),
so every test either proves a round trip or proves the key order is fixed.
"""

from __future__ import annotations

import pytest

from src.brain import frontmatter as fm


def test_split_no_frontmatter_returns_whole_text_as_body():
    text = "Just a free note, no header at all.\n"
    data, body = fm.split(text)
    assert data == {}
    assert body == text


def test_join_then_split_round_trips_scalars_and_lists():
    data = {
        "id": "abc123", "source": "mem:abc123", "kind": "memory", "type": "fact",
        "level": "semantic", "status": "active", "trust": "agent_assertion",
        "confidence": 0.73, "valid_from": "2025-01-01T00:00:00Z", "project": "Bluehaven",
        "tags": ["ui", "onboarding"], "aliases": [], "pinned": True,
    }
    body = "\nAda prefers dark mode.\n\nSome more text.\n"
    text = fm.join(data, body)
    got_data, got_body = fm.split(text)
    assert got_body == body
    for key, value in data.items():
        assert got_data[key] == value


def test_join_orders_known_keys_per_contract_and_skips_none():
    data = {"updated": "2025-02-01", "id": "x", "valid_until": None, "kind": "memory"}
    text = fm.join(data, "\nbody\n")
    header = text.split("---")[1]
    lines = [ln for ln in header.strip("\n").split("\n") if ln]
    keys = [ln.split(":", 1)[0] for ln in lines]
    # id before kind before updated, per FIELD_ORDER, and valid_until (None) is dropped
    assert keys.index("id") < keys.index("kind") < keys.index("updated")
    assert "valid_until" not in keys


def test_join_appends_unknown_keys_sorted_after_known_ones():
    data = {"id": "x", "zeta_custom": "z", "alpha_custom": "a"}
    text = fm.join(data, "\nbody\n")
    data2, _ = fm.split(text)
    keys = list(data2.keys())
    assert keys.index("id") < keys.index("alpha_custom") < keys.index("zeta_custom")


def test_render_is_deterministic_across_repeated_joins():
    data = {"id": "x", "kind": "note", "tags": ["a", "b", "c"]}
    body = "\nSame content every time.\n"
    outputs = {fm.join(data, body) for _ in range(5)}
    assert len(outputs) == 1


def test_special_characters_in_scalars_round_trip():
    data = {"id": "x", "type": "a value: with colon and #hash and \"quotes\""}
    text = fm.join(data, "\nbody\n")
    data2, _ = fm.split(text)
    assert data2["type"] == data["type"]


def test_malformed_frontmatter_degrades_to_empty_dict_not_a_crash():
    text = "---\nthis is not: [valid, yaml: at all: :\n---\nBody stays readable.\n"
    data, body = fm.split(text)
    assert isinstance(data, dict)
    assert "Body stays readable." in body


def test_empty_frontmatter_dict_still_produces_valid_header():
    text = fm.join({}, "\njust a body\n")
    data, body = fm.split(text)
    assert data == {}
    assert "just a body" in body


# ── the dependency-free fallback path (used when PyYAML is unavailable) ──

@pytest.fixture()
def no_yaml(monkeypatch):
    monkeypatch.setattr(fm, "_yaml", None)


def test_fallback_round_trips_scalars_lists_and_booleans(no_yaml):
    data = {
        "id": "abc", "confidence": 0.5, "pinned": True, "tags": ["a", "b"],
        "aliases": [], "type": "has: a colon and #hash",
    }
    text = fm.join(data, "\nBody here.\n")
    got, body = fm.split(text)
    assert got["id"] == "abc"
    assert got["confidence"] == 0.5
    assert got["pinned"] is True
    assert got["tags"] == ["a", "b"]
    assert got["aliases"] == []
    assert got["type"] == data["type"]
    assert body == "\nBody here.\n"


def test_fallback_and_pyyaml_paths_agree_on_a_simple_document():
    data = {"id": "x", "kind": "note", "tags": ["one", "two"], "pinned": False}
    with_yaml = fm.join(data, "\nbody\n")
    parsed_with_yaml, _ = fm.split(with_yaml)

    fm._yaml = None
    try:
        without_yaml = fm.join(data, "\nbody\n")
        parsed_without_yaml, _ = fm.split(without_yaml)
    finally:
        import yaml as _yaml_mod
        fm._yaml = _yaml_mod

    assert parsed_with_yaml == parsed_without_yaml
