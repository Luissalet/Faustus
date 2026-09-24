"""tests/test_skill_web_accessibility_design.py — the bundled
`web-accessibility-design` skill (feature 3a).

Beyond the generic per-skill checks in `test_skill_library.py`, this locks
in two things specific to this skill:

* the NOTICE file with the third-party MIT attribution ships alongside it
  and names its source with a real URL;
* its "## Reference: ..." section (an unrecognized heading, so it lives in
  `body_extra`) survives a full `to_markdown()` round-trip verbatim — this
  is also a regression test for a real bug found while writing this skill:
  `parse_body()`/`emit_body()` used to drop the heading of any non-standard
  section, so a re-parse silently fused that whole section into whatever
  known section preceded it (see `services/memory/skill_format.py`).
"""
from __future__ import annotations

import os

import pytest

from services.memory.skill_format import Skill
from src import skill_library

SLUG = "web-accessibility-design"
SKILL_DIR = os.path.join(skill_library.LIBRARY_DIR, SLUG)


def _read() -> str:
    with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


def test_notice_file_ships_with_the_skill_and_names_its_source():
    notice_path = os.path.join(SKILL_DIR, "NOTICE")
    assert os.path.isfile(notice_path), "web-accessibility-design must ship a NOTICE file"
    with open(notice_path, encoding="utf-8") as fh:
        notice = fh.read()
    assert "MIT" in notice
    assert "github.com" in notice
    assert "Copyright" in notice


def test_reference_section_is_captured_as_body_extra():
    sk = Skill.from_markdown(_read(), path=os.path.join(SKILL_DIR, "SKILL.md"))
    assert "## Reference" in sk.body_extra
    assert "Accessible names" in sk.body_extra
    assert "PWA" in sk.body_extra


def test_reference_section_survives_a_to_markdown_round_trip():
    sk = Skill.from_markdown(_read(), path=os.path.join(SKILL_DIR, "SKILL.md"))
    reparsed = Skill.from_markdown(sk.to_markdown())
    assert reparsed.body_extra == sk.body_extra
    # And the known sections right before it were not swallowed into it.
    assert reparsed.verification == sk.verification
    assert len(reparsed.verification) >= 4


def test_reference_round_trip_is_stable_across_multiple_saves():
    sk = Skill.from_markdown(_read(), path=os.path.join(SKILL_DIR, "SKILL.md"))
    first = sk.to_markdown()
    second = Skill.from_markdown(first).to_markdown()
    assert first == second


def test_install_into_a_user_store_preserves_the_reference_section(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(skill_library, "DATA_DIR", str(data_dir), raising=False)

    result = skill_library.install("alice", [SLUG])
    assert result["results"][0]["status"] == "installed"

    from services.memory.skills import SkillsManager
    mgr = SkillsManager(str(data_dir))
    matches = [s for s in mgr.load(owner="alice") if f"library:{SLUG}" in (s.get("tags") or [])]
    assert len(matches) == 1
    installed_sk = Skill.from_markdown(
        open(matches[0]["path"], encoding="utf-8").read(), path=matches[0]["path"]
    )
    assert "## Reference" in installed_sk.body_extra
    assert "PWA" in installed_sk.body_extra
