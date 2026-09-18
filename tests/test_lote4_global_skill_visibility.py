"""FAUSTUS §115 — a skill installed for every user (`owner: "*"` in its
SKILL.md frontmatter, e.g. a machine-wide `data/skills/general/<name>/
SKILL.md` procedure for long/repetitive agent tasks) must actually reach
`SkillsManager.load(owner=<any real user>)` / `index_for`, instead of being
silently hidden by the strict per-owner filter that
`test_skills_manager_owner_isolation.py` locked in.

Before this fix, `load()` only ever returned `s.get("owner") == owner`, so a
skill written once for the whole install (no single user "owns" it) never
reached ANY user's prompt — a real user's `owner:"*"` would either match no
one (if they typed literal "*", nonsensical) or, if left blank, was already
excluded on purpose (the ownerless-skill leak that isolation test's SECURITY
note describes). `GLOBAL_SKILL_OWNER = "*"` is the deliberate, opt-in escape
hatch: declared explicitly on disk, never inferred from a blank field.
"""

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

for _mod in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

from services.memory.skills import SkillsManager, GLOBAL_SKILL_OWNER  # noqa: E402
from services.memory.skill_format import slugify  # noqa: E402


def _write_skill_md(skills_root: Path, category: str, name: str,
                     owner: str, description: str, status: str = "published") -> Path:
    skill_dir = skills_root / slugify(category or "general", fallback="general") / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = textwrap.dedent(f"""\
        ---
        name: {name}
        description: {description}
        version: 1.0.0
        category: {category}
        tags: []
        status: {status}
        confidence: 0.9
        source: learned
        owner: {owner}
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        long or repetitive tasks

        # Procedure
        - do one unit at a time, persist a cursor
        """)
    path = skill_dir / "SKILL.md"
    path.write_text(md, encoding="utf-8")
    return path


def test_global_skill_owner_sentinel_is_star():
    assert GLOBAL_SKILL_OWNER == "*"


def test_load_includes_global_skill_for_any_real_user(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(
        skills_root, category="general", name="iterative-cursor-loop",
        owner=GLOBAL_SKILL_OWNER,
        description="para trabajos largos y repetitivos usar el bucle con cursor",
    )
    _write_skill_md(
        skills_root, category="alice-cat", name="login-flow",
        owner="alice", description="alice only",
    )

    sm = SkillsManager(str(tmp_path))

    alice_skills = {s["name"] for s in sm.load(owner="alice")}
    luis_skills = {s["name"] for s in sm.load(owner="luis")}

    assert "iterative-cursor-loop" in alice_skills, (
        "global skill (owner='*') must be visible to alice"
    )
    assert "iterative-cursor-loop" in luis_skills, (
        "global skill (owner='*') must be visible to any other real user too"
    )
    assert "login-flow" in alice_skills
    assert "login-flow" not in luis_skills, (
        "a skill owned by a specific user must stay invisible to a different user"
    )


def test_load_still_hides_blank_owner_skill(tmp_path):
    """The pre-existing security fix (ownerless skills hidden) must not be
    reopened: only the explicit '*' sentinel is global, never a blank
    owner field."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(
        skills_root, category="general", name="legacy-unstamped",
        owner="", description="no owner declared",
    )
    sm = SkillsManager(str(tmp_path))
    luis_skills = {s["name"] for s in sm.load(owner="luis")}
    assert "legacy-unstamped" not in luis_skills


def test_index_for_surfaces_global_published_skill(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(
        skills_root, category="general", name="iterative-cursor-loop",
        owner=GLOBAL_SKILL_OWNER,
        description="para trabajos largos y repetitivos usar el bucle con cursor",
        status="published",
    )
    sm = SkillsManager(str(tmp_path))
    idx = sm.index_for(owner="luis")
    assert any(s["name"] == "iterative-cursor-loop" for s in idx)


def test_get_relevant_skills_matches_global_skill_for_repetitive_task(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(
        skills_root, category="general", name="iterative-cursor-loop",
        owner=GLOBAL_SKILL_OWNER,
        description="long repetitive task iterative cursor loop one unit at a time persist state",
        status="published",
    )
    sm = SkillsManager(str(tmp_path))
    hits = sm.get_relevant_skills(
        "create one folder per pokemon evolution line, one unit at a time, "
        "this is a long repetitive task, persist an iterative cursor",
        skills=sm.load(owner="luis"),
        threshold=0.05,
        max_items=3,
        min_confidence=0.0,
    )
    assert any(h.get("name") == "iterative-cursor-loop" for h in hits)
