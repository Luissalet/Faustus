"""src/personas/loader.py — one PERSONA.md -> Persona, or a clear parse error.

Reuses `services/memory/skill_format.py`'s frontmatter parser/emitter and
slugifier — the SAME YAML-subset loader `src/agent_defs.py` uses for
AGENT.md, so there is exactly one frontmatter dialect in this codebase, not
a second one invented for personas.

Frontmatter shape:

    ---
    name: Backend Python Engineer
    slug: backend-python            # optional; derived from the filename/name otherwise
    division: engineering
    summary: One line for a picker list.
    tags: [python, fastapi, backend]
    tools_hint: [read_file, write_file, bash, grep]
    language: en                    # en | es
    ---

    Body: identity + mission + workflow + deliverables + metrics, in prose.

`tags` and `tools_hint` are free-text; `tools_hint` is deliberately NOT
checked against `src.agent_defs.known_tools()` — it is a hint a reader (or a
prompt) uses to decide what to reach for, not a grant, and a persona must
never fail to load because the build it ships with happens not to have a
tool that day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from services.memory.skill_format import emit_frontmatter, parse_frontmatter, slugify

DIVISIONS: Tuple[str, ...] = (
    "engineering", "writing", "research", "product", "design", "data",
    "security", "operations",
)
LANGUAGES: Tuple[str, ...] = ("en", "es")
DEFAULT_LANGUAGE = "en"

_MAX_NAME = 80
_MAX_SUMMARY = 240
_MAX_TAGS = 20
_MAX_BODY_WORDS = 350  # the contract's own ceiling for a builtin persona


class PersonaError(ValueError):
    """A PERSONA.md that will not load, with the reason a human needs."""


@dataclass
class Persona:
    slug: str
    name: str = ""
    division: str = ""
    summary: str = ""
    tags: Tuple[str, ...] = ()
    tools_hint: Tuple[str, ...] = ()
    language: str = DEFAULT_LANGUAGE
    body: str = ""
    source: str = "builtin"          # "builtin" | "user"
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name, "division": self.division,
            "summary": self.summary, "tags": list(self.tags),
            "tools_hint": list(self.tools_hint), "language": self.language,
            "body": self.body, "source": self.source, "path": self.path,
            "word_count": len(self.body.split()),
        }


def _as_str_list(value: Any, fieldname: str, limit: int) -> List[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        if not isinstance(item, (str, int, float)):
            raise PersonaError(f"{fieldname}: expected a list of words, got {type(item).__name__}")
        text = str(item).strip()
        if text:
            out.append(text)
    return out[:limit]


def parse(text: str, *, slug_hint: str = "", source: str = "user", path: str = "") -> Persona:
    """One PERSONA.md -> :class:`Persona`. Raises :class:`PersonaError` on any
    malformed field — a persona that silently drops a field it could not
    parse is a definition nobody knows is incomplete."""
    fm, body = parse_frontmatter(text or "")
    if not isinstance(fm, dict) or not fm:
        raise PersonaError("no frontmatter: a PERSONA.md starts with a `---` block")

    name = str(fm.get("name") or "").strip()[:_MAX_NAME]
    if not name:
        raise PersonaError("name: required")

    slug = slugify(str(fm.get("slug") or slug_hint or name), fallback="")
    if not slug:
        raise PersonaError("slug: could not derive a usable slug from `slug`/`name`/the filename")

    division = str(fm.get("division") or "").strip().lower()
    if division and division not in DIVISIONS:
        raise PersonaError(f"division: `{division}` is not one of {', '.join(DIVISIONS)}")

    summary = str(fm.get("summary") or "").strip()[:_MAX_SUMMARY]

    language = str(fm.get("language") or DEFAULT_LANGUAGE).strip().lower()
    if language not in LANGUAGES:
        raise PersonaError(f"language: `{language}` is not one of {', '.join(LANGUAGES)}")

    tags = tuple(_as_str_list(fm.get("tags"), "tags", _MAX_TAGS))
    tools_hint = tuple(_as_str_list(fm.get("tools_hint"), "tools_hint", _MAX_TAGS))

    body_text = (body or "").strip()
    if not body_text:
        raise PersonaError("body: a persona needs identity/mission/workflow prose, not just frontmatter")

    return Persona(
        slug=slug, name=name, division=division, summary=summary,
        tags=tags, tools_hint=tools_hint, language=language,
        body=body_text, source=source, path=path,
    )


def to_markdown(p: Persona) -> str:
    fm: Dict[str, Any] = {
        "name": p.name,
        "division": p.division,
        "summary": p.summary,
        "tags": list(p.tags),
        "tools_hint": list(p.tools_hint),
        "language": p.language,
    }
    return f"---\n{emit_frontmatter(fm)}\n---\n\n{p.body.strip()}\n"


__all__ = ["DEFAULT_LANGUAGE", "DIVISIONS", "LANGUAGES", "Persona", "PersonaError", "parse", "to_markdown"]
