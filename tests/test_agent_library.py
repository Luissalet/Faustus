"""tests/test_agent_library.py — the bundled agent library (lot C).

Covers: every `config/agents/library/*.md` loads through `src.agent_defs`'s
normal loader (no second dialect), no slug collides with an existing
builtin/profile, every reviewer-mode definition truly cannot write, and no
file names the source pack.
"""
from __future__ import annotations

import os
import re

from src import agent_defs

FORBIDDEN_RE = re.compile(
    r"(?i)claude.code|\becc\b|everything.claude|homunculus|instinct-cli|\.claude/"
)


def _library_slugs():
    return sorted(
        os.path.splitext(name)[0] for name in os.listdir(agent_defs.LIBRARY_DIR)
        if name.lower().endswith(".md") and not name.startswith(".")
    )


def test_at_least_fifteen_agents_are_bundled():
    assert len(_library_slugs()) >= 15


def test_every_bundled_agent_loads_with_no_error():
    result = agent_defs.load_all()
    by_slug = result.by_slug()
    library_slugs = _library_slugs()
    for slug in library_slugs:
        assert slug in by_slug, (
            f"`{slug}` did not load; errors: "
            f"{[e for e in result.errors if e.get('slug') == slug]}"
        )
    # No library file was reported as shadowing (or shadowed by) an existing
    # built-in/profile slug.
    shadow_errors = [e for e in result.errors if "shadows" in e.get("reason", "")]
    assert not shadow_errors, shadow_errors


def test_no_bundled_agent_slug_collides_with_an_existing_builtin_or_profile():
    existing = {d.slug for d in agent_defs.builtins()}
    try:
        from src.agent_profiles import builtin as _profiles
        existing |= {d.slug for d in _profiles.profile_defs()}
    except Exception:
        pass
    collisions = set(_library_slugs()) & existing
    assert not collisions, f"bundled agent slugs collide with existing ones: {collisions}"


def test_every_reviewer_mode_agent_cannot_write():
    result = agent_defs.load_all()
    by_slug = result.by_slug()
    for slug in _library_slugs():
        d = by_slug[slug]
        if d.mode != "reviewer":
            continue
        write_tools = {"write_file", "edit_file", "apply_patch", "bash", "python", "powershell"}
        assert not (write_tools & set(d.tools)), f"`{slug}` is a reviewer but tools include a write tool"
        assert write_tools & set(d.deny), f"`{slug}` is a reviewer but does not deny the write tools"
        assert any(r.action == "write" and r.effect == "deny" and r.pattern == "**"
                  for r in d.permission), f"`{slug}` is a reviewer but has no blanket write-deny rule"


def test_every_bundled_agent_description_has_a_use_when_trigger():
    for slug in _library_slugs():
        path = os.path.join(agent_defs.LIBRARY_DIR, slug + ".md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        d = agent_defs.parse(text, slug=slug, source=agent_defs.SOURCE_BUILTIN, path=path)
        assert "use when" in d.description.lower(), f"`{slug}` description has no 'Use when' clause"


def test_no_bundled_agent_names_the_source_pack():
    for slug in _library_slugs():
        path = os.path.join(agent_defs.LIBRARY_DIR, slug + ".md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert not FORBIDDEN_RE.search(text), f"`{slug}` contains a forbidden word/path"


def test_a_disposable_fixture_folder_can_replace_the_library_dir(tmp_path, monkeypatch):
    """The loader reads `agent_defs.LIBRARY_DIR` as a module global at call
    time, same as `DATA_DIR` — a test can repoint it without touching the
    real bundled files."""
    fixture = tmp_path / "library"
    fixture.mkdir()
    (fixture / "example-fixture-agent.md").write_text(
        "---\nname: example-fixture-agent\ndescription: A fixture. Use when testing.\n"
        "mode: worker\ntools: [read_file]\n---\n\nDo the fixture thing.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_defs, "LIBRARY_DIR", str(fixture))
    result = agent_defs.load_all()
    assert "example-fixture-agent" in result.by_slug()
