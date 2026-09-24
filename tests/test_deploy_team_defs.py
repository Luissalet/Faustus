"""tests/test_deploy_team_defs.py -- the deployment review team library
agents (config/agents/library/deploy-*.md) and their skill (lot C).
"""
from __future__ import annotations

import os

import pytest

from src import agent_defs

_LIBRARY_DIR = agent_defs.LIBRARY_DIR

_DEPLOY_AGENTS = [
    "deploy-code-reviewer",
    "deploy-dependency-auditor",
    "deploy-ci-analyst",
    "deploy-log-monitor",
    "deploy-lead",
]


def _load(slug: str) -> agent_defs.AgentDef:
    path = os.path.join(_LIBRARY_DIR, f"{slug}.md")
    text = open(path, encoding="utf-8").read()
    return agent_defs.parse(text, slug=slug, source=agent_defs.SOURCE_BUILTIN, path=path)


@pytest.mark.parametrize("slug", _DEPLOY_AGENTS)
def test_agent_file_exists(slug):
    assert os.path.isfile(os.path.join(_LIBRARY_DIR, f"{slug}.md"))


@pytest.mark.parametrize("slug", _DEPLOY_AGENTS)
def test_agent_parses_without_caveats(slug):
    d = _load(slug)
    assert d.name == slug
    assert d.description
    assert d.caveats == ()


@pytest.mark.parametrize("slug", _DEPLOY_AGENTS)
def test_agent_tools_are_all_known(slug):
    known = agent_defs.known_tools()
    d = _load(slug)
    assert d.tools, f"{slug} declares no tools"
    unknown = set(d.tools) - known
    assert not unknown, f"{slug} names unknown tools: {unknown}"


def test_worker_and_reviewer_agents_cannot_delegate():
    for slug in ("deploy-code-reviewer", "deploy-dependency-auditor",
                 "deploy-ci-analyst", "deploy-log-monitor"):
        d = _load(slug)
        assert "delegate_agents" not in d.tools
        assert any(r.action == "delegate" and r.effect == "deny" for r in d.permission)


def test_reviewer_agents_cannot_write():
    for slug in ("deploy-code-reviewer", "deploy-ci-analyst", "deploy-log-monitor"):
        d = _load(slug)
        assert d.mode == "reviewer"
        for write_tool in ("write_file", "edit_file", "apply_patch"):
            assert write_tool not in d.tools


def test_dependency_auditor_has_bash_but_no_write_tools():
    d = _load("deploy-dependency-auditor")
    assert "bash" in d.tools
    for write_tool in ("write_file", "edit_file", "apply_patch"):
        assert write_tool not in d.tools


def test_ci_analyst_uses_ci_failures_tool():
    d = _load("deploy-ci-analyst")
    assert "ci_failures" in d.tools


def test_deploy_lead_is_a_coordinator_that_may_delegate():
    d = _load("deploy-lead")
    assert d.mode == "coordinator"
    assert "delegate_agents" in d.tools
    assert any(r.action == "delegate" and r.effect == "allow" for r in d.permission)


def test_deploy_lead_instructions_name_all_four_and_the_hub():
    d = _load("deploy-lead")
    for slug in ("deploy-code-reviewer", "deploy-dependency-auditor",
                 "deploy-ci-analyst", "deploy-log-monitor"):
        assert slug in d.prompt
    assert "Collaboration hub" in d.prompt
    assert "action list" in d.prompt.lower()


def test_all_five_slugs_are_distinct_and_slug_matches_filename():
    seen = set()
    for slug in _DEPLOY_AGENTS:
        d = _load(slug)
        assert d.name == slug
        assert slug not in seen
        seen.add(slug)


def test_library_loader_finds_all_five():
    """The same discovery `src.agent_defs.list_definitions` (or the
    resolve path `delegate_agents` uses) walks -- not just a direct parse
    of the file, so an agent named in a delegation task actually resolves."""
    from src import agent_defs as ad
    errors = ad.resolve_tasks(
        [{"agent": slug, "instruction": "smoke"} for slug in _DEPLOY_AGENTS],
        workspace=None,
    )
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Skill: skills/library/deployment-review/SKILL.md
# ---------------------------------------------------------------------------

_SKILL_PATH = "skills/library/deployment-review/SKILL.md"


def test_skill_file_exists():
    assert os.path.isfile(_SKILL_PATH)


def test_skill_parses_with_required_sections():
    from services.memory.skill_format import parse_frontmatter
    text = open(_SKILL_PATH, encoding="utf-8").read()
    fm, body = parse_frontmatter(text)
    assert fm["name"] == "deployment-review"
    assert fm["status"] == "published"
    assert "Use when" in fm["description"]
    for heading in ("## When to Use", "## Procedure", "## Pitfalls", "## Verification"):
        assert heading in body
    word_count = len(body.split())
    assert 400 <= word_count <= 1500


def test_skill_mentions_delegate_agents_and_deploy_lead():
    text = open(_SKILL_PATH, encoding="utf-8").read()
    assert "delegate_agents" in text
    assert "deploy-lead" in text
    for slug in ("deploy-code-reviewer", "deploy-dependency-auditor",
                 "deploy-ci-analyst", "deploy-log-monitor"):
        assert slug in text


def test_skill_library_discovers_it():
    from src import skill_library
    names = {s["slug"] for s in skill_library.list_library()}
    assert "deployment-review" in names
