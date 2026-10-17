"""A library install must remain useful when the source directory is gone."""
import json

import pytest

from services.memory.skills import SkillsManager
from src import skill_library


def _source(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "library"
    source = library / "fixture"
    (source / "references").mkdir(parents=True)
    (source / "templates").mkdir()
    text = """---
name: portable-fixture
description: Read a fixture. Use when checking a portable bundle.
category: testing
status: published
source: imported
---

## When to Use
Use when checking a portable bundle.

## Procedure
1. Read [the fixture](references/input.md).

## Pitfalls
- Do not guess the fixture's value.

## Verification
- Return the actual fixture value.
"""
    (source / "SKILL.md").write_text(text, encoding="utf-8")
    (source / "references" / "input.md").write_text("árbol: 73129\n", encoding="utf-8")
    (source / "templates" / "result.json").write_text('{"value": null}\n', encoding="utf-8")
    (source / "NOTICE").write_text("Fixture provenance\n", encoding="utf-8")
    monkeypatch.setattr(skill_library, "LIBRARY_DIR", str(library))
    monkeypatch.setattr(skill_library, "DATA_DIR", str(data))
    return source, SkillsManager(str(data))


def test_library_bundle_survives_without_its_source(tmp_path, monkeypatch):
    source, manager = _source(tmp_path, monkeypatch)
    result = skill_library.install("reader", ["fixture"])["results"][0]
    assert result["status"] == "installed"
    name = result["name"]
    source.rename(source.with_name("unavailable"))
    assert manager.read_skill_reference(name, "references/input.md", owner="reader") == "árbol: 73129\n"
    assert manager.read_skill_reference(name, "templates/result.json", owner="reader") == '{"value": null}\n'
    assert manager.read_skill_reference(name, "NOTICE", owner="reader") == "Fixture provenance\n"
    assert manager.read_skill_reference(name, "references/input.md", owner="someone-else") is None


def test_unreadable_resource_does_not_remove_the_previous_install(tmp_path, monkeypatch):
    source, manager = _source(tmp_path, monkeypatch)
    first = skill_library.install("reader", ["fixture"])["results"][0]
    (source / "references" / "input.md").write_bytes(b"\xff\xfe\x00")
    result = skill_library.install("reader", ["fixture"], replace=True)["results"][0]
    assert result["status"] == "error"
    assert manager.read_skill_reference(first["name"], "references/input.md", owner="reader") == "árbol: 73129\n"
    assert len(manager.load(owner="reader")) == 1


@pytest.mark.parametrize("slug", [
    "reverse-engineering-workflow", "blender-asset-production",
    "deterministic-motion-production", "revision-aware-media",
    "code-impact-analysis", "entity-investigation",
    "historical-evidence-research", "local-voice-comparison",
])
def test_local_workflow_installs_its_worked_scenario(tmp_path, monkeypatch, slug):
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path))
    result = skill_library.install("reader", [slug])["results"][0]
    assert result["status"] == "installed"
    manager = SkillsManager(str(tmp_path))
    reference = manager.read_skill_reference(
        result["name"], "references/scenario.json", owner="reader")
    assert isinstance(json.loads(reference), dict)
