"""
skills_runtime — the bridge between the skills Faustus has and the contract it
now speaks.

Phase 2 of the masterplan asks for a runtime that can install, authorize, run,
cancel, version and uninstall one skill safely, before anyone writes a hundred
of them. This package is the first half of that: making the skills that exist
today — `SKILL.md` files with YAML frontmatter — describe themselves in the
same vocabulary a manifest does, so the router, the permission check and the
approval cards apply to them without a second code path.

Two rules hold the whole thing up.

**Deny by default, with no exception for the trusted-looking.** A `SKILL.md`
that says nothing about permissions gets none, which means no backend, which
means it cannot run anything. That reads like a bug the first time you see it
and it is the point: today's skills were written as instructions for a model,
not as capabilities, and treating a document as if it had asked for the disk
because it did not say otherwise is how a skills folder becomes an attack
surface.

**Provenance never elevates.** A skill found in `.claude/skills` gets exactly
what a skill found in `data/skills` gets. Where a file sits on disk is not a
statement about what it may do, and the moment it is, the way to get
permissions is to put a file in the right folder.
"""

from .bridge import (  # noqa: F401
    CAPABILITY_KEYS, SkillBridgeResult, manifest_from_markdown,
    manifest_from_skill, permissions_from_frontmatter, survey,
)
from .discovery import (  # noqa: F401
    SKILL_DIR_NAMES, DiscoveredSkill, discover, load, roots_for, shadowed,
)

__all__ = [
    "manifest_from_markdown", "manifest_from_skill", "permissions_from_frontmatter",
    "survey", "SkillBridgeResult", "CAPABILITY_KEYS",
    "discover", "load", "roots_for", "shadowed", "DiscoveredSkill", "SKILL_DIR_NAMES",
]
