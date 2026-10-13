"""External skill requirements survive native parsing and persistence."""
import pytest

from services.memory.skill_format import Skill, parse_frontmatter


FLOW_SKILL = '''---
name: workspace-tools
description: Use the workspace command line tools.
metadata:
  {
    "openclaw": {
      "requires": { "bins": ["workspace-cli"], "env": ["WORKSPACE_TOKEN"], },
      "install": [{ "kind": "brew", "formula": "workspace-cli", "bins": ["workspace-cli"], },],
      "os": ["darwin", "linux"],
      "literal": "commas ,} and ,] stay as written",
    },
  }
license: MIT
---

# Workspace tools

Use the existing authenticated command line tool.
'''


def test_external_multiline_flow_metadata_survives():
    skill = Skill.from_markdown(FLOW_SKILL)
    meta = skill.extra["metadata"]["openclaw"]
    assert meta["requires"] == {"bins": ["workspace-cli"], "env": ["WORKSPACE_TOKEN"]}
    assert meta["install"][0]["bins"] == ["workspace-cli"]
    assert meta["literal"] == "commas ,} and ,] stay as written"
    assert skill.extra["license"] == "MIT"
    assert Skill.from_markdown(skill.to_markdown()).extra == skill.extra


@pytest.mark.parametrize("opening", ['metadata: {"external":', 'metadata:\n  {"external":'])
def test_flow_mapping_root_key_boundary(opening):
    fm, body = parse_frontmatter('---\n' + opening + '\n    {"bins": ["tool"]}}\nlicense: MIT\n---\nbody')
    assert fm == {"metadata": {"external": {"bins": ["tool"]}}, "license": "MIT"}
    assert body == "body"


def test_nested_mapping_round_trip_keeps_types_and_escaped_strings():
    metadata = {"external": {"empty": [], "off": False, "none": None,
                 "id": "00123", "quotes": 'a "quoted" value', "line": "a\u2028b",
                 "objects": [{"name": "a,b", "args": {"x": 2}}]}}
    skill = Skill(name="nested", extra={"metadata": metadata})
    for _ in range(3):
        skill = Skill.from_markdown(skill.to_markdown())
        assert skill.extra["metadata"] == metadata


def test_invalid_flow_metadata_is_preserved_as_text():
    fm, _ = parse_frontmatter('---\nname: x\nmetadata: {not valid json}\nlicense: MIT\n---\n')
    assert fm["metadata"] == "{not valid json}"
    assert fm["license"] == "MIT"


def test_list_of_nested_objects_and_escaped_quotes_round_trip():
    extra = {"recipes": [{"args": {"id": "a,b", "label": 'say "yes", then go'}, "enabled": True}]}
    skill = Skill(name="recipes", extra=extra)
    assert Skill.from_markdown(skill.to_markdown()).extra == extra


def test_bundle_import_persists_requirements_and_resources(tmp_path):
    from services.memory.skills import SkillsManager
    manager = SkillsManager(data_dir=str(tmp_path))
    result = manager.import_bundle_from_files({"SKILL.md": FLOW_SKILL, "references/example.txt": "resource"}, owner="qa")
    assert result["extra"]["metadata"]["openclaw"]["requires"]["bins"] == ["workspace-cli"]
    saved = Skill.from_markdown(open(result["path"], encoding="utf-8").read())
    assert saved.extra == result["extra"]
    assert manager.load(owner="qa")[0]["extra"] == result["extra"]
