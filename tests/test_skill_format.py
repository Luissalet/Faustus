from services.memory.skill_format import Skill


def test_portable_metadata_can_publish_a_library_skill():
    skill = Skill.from_markdown('''---
name: portable-game
description: Develop a game.
metadata:
  status: published
  category: engineering
  version: 2.0.0
---
Game procedure.
''')
    assert skill.status == "published" and skill.category == "engineering"
    assert skill.version == "2.0.0"
    assert skill.extra["metadata"]["status"] == "published"


def test_native_fields_override_portable_metadata_without_inheriting_owner():
    skill = Skill.from_markdown('''---
name: owned-game
status: draft
metadata:
  status: published
  owner: different-user
---
Game procedure.
''')
    assert skill.status == "draft"
    assert skill.owner is None
    assert Skill.from_markdown(skill.to_markdown()).status == "draft"
