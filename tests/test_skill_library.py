"""tests/test_skill_library.py — the bundled skill library (lot C).

Covers: every `skills/library/<slug>/SKILL.md` parses, has the four
required sections, an explicit "Use when" trigger clause in its
description, stays within the word cap, and carries none of the source
pack's own words; install/uninstall against a disposable data dir is
idempotent; the routes work end to end.
"""
from __future__ import annotations

import os
import re

import pytest

from services.memory.skill_format import Skill
from src import skill_library

FORBIDDEN_RE = re.compile(
    r"(?i)claude.code|\becc\b|everything.claude|homunculus|instinct-cli|\.claude/"
)

LIBRARY_DIR = skill_library.LIBRARY_DIR


def _slugs():
    return sorted(
        name for name in os.listdir(LIBRARY_DIR)
        if os.path.isfile(os.path.join(LIBRARY_DIR, name, "SKILL.md"))
    )


# ---------------------------------------------------------------------------
# Content shape
# ---------------------------------------------------------------------------

def test_at_least_twenty_five_skills_are_bundled():
    assert len(_slugs()) >= 25


@pytest.mark.parametrize("slug", _slugs())
def test_every_bundled_skill_has_the_four_required_sections(slug):
    path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    sk = Skill.from_markdown(text, path=path)
    assert sk.when_to_use.strip(), f"{slug}: missing '## When to Use'"
    assert sk.procedure, f"{slug}: missing '## Procedure'"
    assert sk.pitfalls, f"{slug}: missing '## Pitfalls'"
    assert sk.verification, f"{slug}: missing '## Verification'"
    assert sk.status == "published"
    assert sk.source == "imported"
    assert sk.category in ("engineering", "testing", "security", "research", "planning", "writing"), (
        f"{slug}: category {sk.category!r} is not one of the allowed six"
    )


@pytest.mark.parametrize("slug", _slugs())
def test_every_bundled_skill_description_has_a_use_when_trigger(slug):
    path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    sk = Skill.from_markdown(text, path=path)
    assert "use when" in sk.description.lower(), (
        f"{slug}: description has no explicit 'Use when …' trigger clause"
    )


@pytest.mark.parametrize("slug", _slugs())
def test_every_bundled_skill_is_within_the_word_cap(slug):
    path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    words = len(text.split())
    assert 200 <= words <= 1500, f"{slug}: {words} words is outside the target range"


@pytest.mark.parametrize("slug", _slugs())
def test_no_bundled_skill_names_the_source_pack(slug):
    path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert not FORBIDDEN_RE.search(text), f"{slug}: contains a forbidden word/path"


def test_readme_does_not_name_the_source_pack():
    path = os.path.join(LIBRARY_DIR, "README.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert not FORBIDDEN_RE.search(text)


def test_list_library_never_raises_on_a_malformed_file(tmp_path, monkeypatch):
    bad_dir = tmp_path / "library"
    (bad_dir / "broken").mkdir(parents=True)
    (bad_dir / "broken" / "SKILL.md").write_text("not frontmatter at all", encoding="utf-8")
    monkeypatch.setattr(skill_library, "LIBRARY_DIR", str(bad_dir))
    rows = skill_library.list_library()
    assert len(rows) == 1
    # A file with no frontmatter still parses (Skill.from_markdown never
    # raises); it just has an empty description/category, so it is
    # reported as a normal (if useless) row, not an `error` row.
    assert "slug" in rows[0]


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(skill_library, "DATA_DIR", str(data_dir), raising=False)
    return data_dir


def test_install_then_installed_reports_the_slug(isolated_store):
    slug = _slugs()[0]
    result = skill_library.install("alice", [slug])
    assert result["results"][0]["status"] == "installed"
    assert slug in skill_library.installed("alice")


def test_install_is_idempotent_without_replace(isolated_store):
    slug = _slugs()[0]
    skill_library.install("alice", [slug])
    second = skill_library.install("alice", [slug])
    assert second["results"][0]["status"] == "skipped"
    # Only one copy exists on disk for this owner.
    from services.memory.skills import SkillsManager
    mgr = SkillsManager(str(isolated_store))
    matching = [s for s in mgr.load(owner="alice")
                if f"library:{slug}" in (s.get("tags") or [])]
    assert len(matching) == 1


def test_install_with_replace_reinstalls(isolated_store):
    """Replace means replace: seen live, the second install landed beside
    the first under a `-2` name and both stayed installed."""
    slug = _slugs()[0]
    first = skill_library.install("alice", [slug])
    second = skill_library.install("alice", [slug], replace=True)
    assert second["results"][0]["status"] == "installed"
    assert second["results"][0]["name"] == first["results"][0]["name"]
    assert second["results"][0].get("replaced") == 1
    mgr = skill_library._skills_manager()
    copies = [s for s in mgr.load(owner="alice") if f"library:{slug}" in (s.get("tags") or [])]
    assert len(copies) == 1


def test_uninstall_removes_the_tagged_copy_and_is_idempotent(isolated_store):
    slug = _slugs()[0]
    skill_library.install("alice", [slug])
    first = skill_library.uninstall("alice", [slug])
    assert first["results"][0]["status"] == "removed"
    assert slug not in skill_library.installed("alice")
    second = skill_library.uninstall("alice", [slug])
    assert second["results"][0]["status"] == "not_installed"


def test_install_is_scoped_per_owner(isolated_store):
    slug = _slugs()[0]
    skill_library.install("alice", [slug])
    assert slug not in skill_library.installed("bob")
    rows = skill_library.list_library(owner="bob")
    row = next(r for r in rows if r["slug"] == slug)
    assert row["installed_for"] is False


def test_install_refuses_a_critical_security_finding(isolated_store, monkeypatch):
    from src import skill_import_review

    monkeypatch.setattr(
        skill_import_review, "scan_skill_folder",
        lambda skill_dir: {"risk_level": "critical", "findings": []},
    )
    slug = _slugs()[0]
    result = skill_library.install("alice", [slug])
    assert result["results"][0]["status"] == "refused"
    assert slug not in skill_library.installed("alice")


def test_install_reports_not_found_for_an_unknown_slug(isolated_store):
    result = skill_library.install("alice", ["this-slug-does-not-exist"])
    assert result["results"][0]["status"] == "not_found"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _build_client(monkeypatch, user="alice"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import skill_library_routes as lib_routes

    monkeypatch.setattr(lib_routes, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(lib_routes.setup_skill_library_routes())
    return TestClient(app)


def test_list_route_returns_the_bundled_skills(isolated_store, monkeypatch):
    client = _build_client(monkeypatch)
    resp = client.get("/api/skills/library")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 25
    assert body["count"] == len(body["skills"])


def test_install_and_uninstall_routes_round_trip(isolated_store, monkeypatch):
    client = _build_client(monkeypatch)
    slug = _slugs()[0]
    resp = client.post("/api/skills/library/install", json={"slugs": [slug]})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "installed"

    resp = client.post("/api/skills/library/uninstall", json={"slugs": [slug]})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "removed"
