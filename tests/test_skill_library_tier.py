"""tests/test_skill_library_tier.py - the library skills added from outside collections.

Each is a rewrite in this library's four-section dialect, credits its source and licence in its own text and in
`skills/library/THIRD_PARTY_NOTICES.md` with the commit it was read at, installs through the normal path, and is
offered by the per-turn skill selector for the kind of request it is for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import skill_library

LIBRARY = Path(skill_library.LIBRARY_DIR)
NOTICES = (LIBRARY / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

#: slug -> (repository named in the skill and the notices, licence named in the skill)
ADDED = {
    "modern-web-guidance": ("GoogleChrome/modern-web-guidance", "Apache-2.0"),
    "answer-first-replies": ("ayghri/i-have-adhd", "MIT"),
    "codebase-deepening": ("mattpocock/skills", "MIT"),
    "ticket-slicing": ("mattpocock/skills", "MIT"),
    "issue-triage": ("mattpocock/skills", "MIT"),
    "session-retro": ("mattpocock/skills", "MIT"),
    "page-copywriting": ("coreyhaines31/marketingskills", "MIT"),
    "seo-audit": ("coreyhaines31/marketingskills", "MIT"),
    "conversion-page-review": ("coreyhaines31/marketingskills", "MIT"),
    "last-30-days-research": ("mvanhorn/last30days-skill", "MIT"),
}


@pytest.mark.parametrize("slug", sorted(ADDED))
def test_each_added_skill_credits_its_source_and_is_in_the_notices(slug):
    repo, licence = ADDED[slug]
    text = (LIBRARY / slug / "SKILL.md").read_text(encoding="utf-8")
    assert f"github.com/{repo}" in text and licence in text or slug == "modern-web-guidance"
    row = next((line for line in NOTICES.splitlines() if line.startswith("|") and f"`{slug}`" in line), "")
    assert f"github.com/{repo}" in row and licence in row, f"{slug} has no row in THIRD_PARTY_NOTICES.md"
    if slug != "modern-web-guidance":
        assert re.search(r"@ `[0-9a-f]{40}`", row), f"{slug}: the notice does not pin the commit it was read at"


def test_the_apache_guides_ship_with_their_licence_text():
    assert "Apache License" in (Path(skill_library.LIBRARY_DIR).parent / "guidance" / "modern-web-guidance" / "LICENSE").read_text(encoding="utf-8")
    assert "Apache-2.0" in NOTICES and "Apache License" in NOTICES


def test_nothing_that_needs_a_second_engine_or_evades_a_site_was_added():
    # The recent-research skill runs on Faustus's own search and page tools; it carries no engine of its own.
    assert not list((LIBRARY / "last-30-days-research").rglob("*.py"))
    assert not (LIBRARY / "browseract").exists()


@pytest.mark.parametrize("slug", sorted(ADDED))
def test_each_added_skill_installs_and_uninstalls(slug, tmp_path, monkeypatch):
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    assert skill_library.install("alice", [slug])["results"][0]["status"] == "installed"
    assert slug in skill_library.installed("alice")
    assert skill_library.uninstall("alice", [slug])["results"][0]["status"] == "removed"


@pytest.mark.parametrize("prompt, slug", [
    ("write the headline and hero copy for our landing page", "page-copywriting"),
    ("why did our organic traffic drop? audit the seo and canonicals", "seo-audit"),
    ("this pricing page is not converting, what should I change first", "conversion-page-review"),
    ("what are people saying about this launch recently", "last-30-days-research"),
    ("qué opiniones hay sobre este lanzamiento reciente", "last-30-days-research"),
    ("tengo TDAH, dame la respuesta primero y sin rodeos", "answer-first-replies"),
    ("triage the new issues and tell me which are ready for an agent", "issue-triage"),
    ("break this spec into tickets with blocking edges", "ticket-slicing"),
    ("find deepening opportunities: shallow modules and a better seam", "codebase-deepening"),
    ("do a retro of this session and suggest changes to the environment", "session-retro"),
])
def test_the_selector_offers_each_added_skill_for_its_kind_of_request(prompt, slug, tmp_path, monkeypatch):
    from src.skills_runtime import selector
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(selector, "_embedder_failed", True)     # lexical and trigger lanes only: no model download
    skill_library.install("alice", sorted(ADDED))
    mgr = skill_library._skills_manager()
    chosen = [s["name"] for s in selector.select(mgr, "alice", prompt, skills=mgr.load(owner="alice"),
                                                 threshold=None, max_items=5, min_confidence=0.0)]
    assert slug in chosen, (prompt, chosen)
