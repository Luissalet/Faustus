"""tests/test_r4_personas.py — R4 (Reach wave): agent personas.

Loading, user override, invalid frontmatter, and render_system_block.
"""
from __future__ import annotations

import os

import pytest

from src.personas import loader, registry


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DATA_DIR", str(tmp_path))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------

def test_parse_valid_persona():
    text = (
        "---\n"
        "name: Test Persona\n"
        "division: engineering\n"
        "summary: A test.\n"
        "tags: [a, b]\n"
        "tools_hint: [read_file]\n"
        "language: en\n"
        "---\n\n"
        "## Identity\n\nSomeone who tests things.\n"
    )
    p = loader.parse(text, slug_hint="test-persona")
    assert p.slug == "test-persona"
    assert p.name == "Test Persona"
    assert p.division == "engineering"
    assert "tags" not in p.to_dict() or p.to_dict()["tags"] == ["a", "b"]
    assert "Someone who tests things" in p.body


def test_parse_requires_frontmatter():
    with pytest.raises(loader.PersonaError):
        loader.parse("just text, no frontmatter")


def test_parse_requires_name():
    text = "---\ndivision: engineering\n---\n\nbody\n"
    with pytest.raises(loader.PersonaError):
        loader.parse(text, slug_hint="no-name")


def test_parse_requires_body():
    text = "---\nname: Empty Body\n---\n\n"
    with pytest.raises(loader.PersonaError):
        loader.parse(text, slug_hint="empty-body")


def test_parse_rejects_unknown_division():
    text = "---\nname: X\ndivision: astrology\n---\n\nbody text here\n"
    with pytest.raises(loader.PersonaError):
        loader.parse(text, slug_hint="x")


def test_parse_rejects_unknown_language():
    text = "---\nname: X\nlanguage: fr\n---\n\nbody text here\n"
    with pytest.raises(loader.PersonaError):
        loader.parse(text, slug_hint="x")


def test_to_markdown_roundtrips():
    text = (
        "---\nname: Round Trip\ndivision: writing\nsummary: s\n"
        "tags: [x]\ntools_hint: [read_file]\nlanguage: es\n---\n\ncuerpo\n"
    )
    p = loader.parse(text, slug_hint="round-trip")
    again = loader.parse(loader.to_markdown(p), slug_hint="round-trip")
    assert again.name == p.name
    assert again.division == p.division
    assert again.tags == p.tags
    assert again.body == p.body


# ---------------------------------------------------------------------------
# registry: builtins load
# ---------------------------------------------------------------------------

def test_all_16_builtins_load():
    result = registry.load_all()
    assert result.errors == []
    assert len(result.personas) == 16
    slugs = {p.slug for p in result.personas}
    assert "backend-python" in slugs
    assert "editor-literario" in slugs


def test_builtin_word_counts_within_budget():
    result = registry.load_all()
    for p in result.personas:
        assert len(p.body.split()) <= 350, f"{p.slug} exceeds 350 words"


def test_literary_personas_are_spanish():
    for slug in ("editor-literario", "guionista", "world-builder"):
        p = registry.get_persona(slug)
        assert p is not None
        assert p.language == "es"


# ---------------------------------------------------------------------------
# registry: user override
# ---------------------------------------------------------------------------

def test_user_persona_overrides_builtin(isolated_data_dir):
    text = (
        "---\nname: Backend Python Engineer (custom)\ndivision: engineering\n"
        "summary: custom override\n---\n\nMy own custom identity text.\n"
    )
    saved = registry.save_user_persona("backend-python", text)
    assert saved.source == "user"
    fetched = registry.get_persona("backend-python")
    assert fetched.source == "user"
    assert "custom" in fetched.summary
    result = registry.load_all()
    assert len([p for p in result.personas if p.slug == "backend-python"]) == 1


def test_user_persona_new_slug_adds_to_catalogue(isolated_data_dir):
    text = "---\nname: My Own Persona\ndivision: research\n---\n\nSome identity text.\n"
    registry.save_user_persona("my-own-persona", text)
    result = registry.load_all()
    assert len(result.personas) == 17
    assert registry.get_persona("my-own-persona") is not None


def test_save_rejects_slug_mismatch(isolated_data_dir):
    text = "---\nname: X\nslug: something-else\n---\n\nbody text\n"
    with pytest.raises(loader.PersonaError):
        registry.save_user_persona("my-slug", text)


def test_save_rejects_invalid_frontmatter(isolated_data_dir):
    with pytest.raises(loader.PersonaError):
        registry.save_user_persona("bad", "no frontmatter here")


def test_delete_user_persona_restores_builtin(isolated_data_dir):
    text = "---\nname: Override\n---\n\nOverride body text here.\n"
    registry.save_user_persona("backend-python", text)
    assert registry.get_persona("backend-python").source == "user"
    deleted = registry.delete_user_persona("backend-python")
    assert deleted is True
    assert registry.get_persona("backend-python").source == "builtin"


def test_delete_nonexistent_user_persona_returns_false(isolated_data_dir):
    assert registry.delete_user_persona("never-existed") is False


# ---------------------------------------------------------------------------
# render_system_block
# ---------------------------------------------------------------------------

def test_render_system_block_known_slug():
    block = registry.render_system_block("code-reviewer")
    assert "Code Reviewer" in block
    assert len(block) > 50


def test_render_system_block_unknown_slug_returns_empty():
    assert registry.render_system_block("does-not-exist") == ""
