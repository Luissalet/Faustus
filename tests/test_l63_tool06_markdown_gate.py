"""L63 · TOOL-06 — `POST /api/skills/{id}/markdown` refuses to mutate an
obsolete/deprecated/superseded skill (docs/spec/v2/backlog.json TOOL-06).

`PUT /api/skills/{id}` (`update_skill`) already ran every write through
`skill_governance.gate_mutating_use`/`validate_promotion` (Lote 50 wiring,
`tests/test_p1_tool06_skill_governance.py` keeps passing unmodified — rule
3). `save_skill_markdown` — a full-content rewrite, just as much a mutation
— did not: an obsolete skill's SKILL.md could still be overwritten wholesale
by pasting new markdown, bypassing the gate entirely. This closes that gap.

Same harness as `tests/test_skill_save_no_rename.py`: route handlers called
directly with a mock Request, no server/network/browser.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.datastructures import State

from services.memory.skill_format import slugify
from services.memory.skills import SkillsManager
from routes.skills_routes import setup_skills_routes


def _write_skill_md(skills_root: Path, name: str, owner: str, status: str) -> Path:
    skill_dir = skills_root / "general" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = textwrap.dedent(f"""\
        ---
        name: {name}
        description: original description
        version: 1.0.0
        category: general
        tags: []
        status: {status}
        confidence: 0.8
        source: learned
        owner: {owner}
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        test

        # Procedure
        - step 1
        """)
    path = skill_dir / "SKILL.md"
    path.write_text(md, encoding="utf-8")
    return path


def _md(name: str, *, status: str = "draft") -> str:
    return textwrap.dedent(f"""\
        ---
        name: {name}
        description: edited description
        version: 1.0.0
        category: general
        tags: []
        status: {status}
        confidence: 0.8
        source: learned
        owner: alice
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        edited

        # Procedure
        - step 1
        """)


def _request(user: str, body: dict) -> Request:
    scope = {"type": "http", "app": type("App", (), {"state": State()})(),
             "state": {"current_user": user}, "headers": []}

    async def _receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request(scope=scope, receive=_receive)


def _handler(router, path: str, method: str):
    return next(r.endpoint for r in router.routes
               if r.path == path and method in r.methods)


@pytest.mark.parametrize("status", ["obsolete", "deprecated", "superseded"])
@pytest.mark.asyncio
async def test_saving_markdown_over_a_retired_skill_is_refused(tmp_path, status):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(skills_root, "old-skill", "alice", status)

    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    save = _handler(router, "/api/skills/{skill_id}/markdown", "POST")

    with pytest.raises(HTTPException) as excinfo:
        await save(_request("alice", {"markdown": _md("old-skill")}), "old-skill")
    assert excinfo.value.status_code == 409

    # Nothing was actually written: the description on disk is unchanged.
    descriptions = {s.get("description") for s in sm.load(owner="alice")}
    assert "edited description" not in descriptions
    assert "original description" in descriptions


@pytest.mark.asyncio
async def test_saving_markdown_over_a_draft_skill_still_works(tmp_path):
    """The gate is additive — a skill in one of today's two real statuses
    (draft/published) is unaffected, exactly as `PUT` already behaves."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    _write_skill_md(skills_root, "active-skill", "alice", "draft")

    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    save = _handler(router, "/api/skills/{skill_id}/markdown", "POST")

    res = await save(_request("alice", {"markdown": _md("active-skill")}), "active-skill")
    assert res["ok"] is True
    descriptions = {s.get("description") for s in sm.load(owner="alice")}
    assert "edited description" in descriptions


@pytest.mark.asyncio
async def test_promoting_via_markdown_without_review_is_refused_for_a_teach_mode_origin(tmp_path):
    """A status change hidden inside pasted markdown is still a promotion —
    a caller cannot skip `validate_promotion` just by not using PUT."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    skill_dir = skills_root / "general" / "taught-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = textwrap.dedent("""\
        ---
        name: taught-skill
        description: demonstrated procedure
        version: 1.0.0
        category: general
        tags: []
        status: draft
        confidence: 0.8
        source: teach_mode
        owner: alice
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        test

        # Procedure
        - step 1
        """)
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")

    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    save = _handler(router, "/api/skills/{skill_id}/markdown", "POST")

    with pytest.raises(HTTPException) as excinfo:
        await save(_request("alice", {"markdown": _md("taught-skill", status="published")}),
                  "taught-skill")
    assert excinfo.value.status_code == 409
