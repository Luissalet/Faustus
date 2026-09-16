"""src/personas/ — agent personas as Markdown files (R4, Reach wave,
agency-agents research note: `agent_repos.md` §5).

A persona is an IDENTITY, not a permission: it is prose (who this agent is,
its mission, its workflow, its deliverables, how it judges its own work)
that gets prepended to a system prompt. It grants no tool, denies no path —
`src/agent_defs.py` already owns every enforcement point (§ its own
docstring), and a persona is designed to be REFERENCED from an AgentDef
(`persona: slug` in an AGENT.md's frontmatter), never a second selector next
to it.

    loader.py    one PERSONA.md -> Persona (frontmatter + body), or a clear
                 parse error
    registry.py  builtin/*.md (16 shipped) + DATA_DIR/personas/*.md (the
                 user's own, overriding a builtin by slug); render_system_block
    builtin/     the 16 shipped personas
"""
from __future__ import annotations

from .loader import Persona, PersonaError, parse, to_markdown
from .registry import (
    delete_user_persona,
    get_persona,
    list_personas,
    render_system_block,
    save_user_persona,
)

__all__ = [
    "Persona", "PersonaError", "parse", "to_markdown",
    "delete_user_persona", "get_persona", "list_personas",
    "render_system_block", "save_user_persona",
]
