"""src/personas/registry.py — the persona catalogue: 16 builtins + the
user's own `DATA_DIR/personas/*.md`, the user's overriding a builtin of the
same slug (same precedence rule `src/agent_defs.py` uses for AGENT.md).

A broken file never takes the catalogue down: it is skipped and reported in
`errors`, the same "never fatal, never silent" rule `agent_defs.load_all`
follows, for the same reason — a persona that quietly disappears from the
list is worse than one that never loaded.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .loader import Persona, PersonaError, parse, to_markdown

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

#: Module-level so tests can point the store somewhere disposable (same
#: pattern `src.agent_defs.DATA_DIR` and `services.experts` use).
DATA_DIR = _DEFAULT_DATA_DIR
PERSONAS_DIRNAME = "personas"

_BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin")
_MAX_FILE_BYTES = 200_000
_MAX_PERSONAS = 200


@dataclass
class LoadResult:
    personas: List[Persona] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)

    def by_slug(self) -> Dict[str, Persona]:
        return {p.slug: p for p in self.personas}


def personas_root() -> str:
    """`DATA_DIR/personas` — one flat file per persona, `<slug>.md`."""
    return os.path.join(DATA_DIR, PERSONAS_DIRNAME)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_MAX_FILE_BYTES)


def _load_builtins(result: LoadResult, seen: Dict[str, int]) -> None:
    try:
        names = sorted(os.listdir(_BUILTIN_DIR))
    except OSError as exc:  # pragma: no cover - the package always ships this dir
        logger.warning("personas: builtin directory unreadable: %s", exc)
        return
    for name in names:
        if not name.lower().endswith(".md"):
            continue
        slug_hint = os.path.splitext(name)[0]
        path = os.path.join(_BUILTIN_DIR, name)
        try:
            persona = parse(_read(path), slug_hint=slug_hint, source="builtin", path=path)
        except PersonaError as exc:
            result.errors.append({"path": path, "slug": slug_hint, "reason": str(exc)})
            logger.warning("personas: builtin %r does not load: %s", slug_hint, exc)
            continue
        except OSError as exc:  # pragma: no cover
            result.errors.append({"path": path, "slug": slug_hint, "reason": f"could not be read: {exc}"})
            continue
        seen[persona.slug] = len(result.personas)
        result.personas.append(persona)


def _load_user(result: LoadResult, seen: Dict[str, int]) -> None:
    root = personas_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return  # no user store yet: the builtins are the whole list
    for name in names[:_MAX_PERSONAS]:
        if not name.lower().endswith(".md") or name.startswith("."):
            continue
        slug_hint = os.path.splitext(name)[0]
        path = os.path.join(root, name)
        try:
            persona = parse(_read(path), slug_hint=slug_hint, source="user", path=path)
        except PersonaError as exc:
            result.errors.append({"path": path, "slug": slug_hint, "reason": str(exc)})
            continue
        except OSError as exc:
            result.errors.append({"path": path, "slug": slug_hint, "reason": f"could not be read: {exc}"})
            continue
        index = seen.get(persona.slug)
        if index is None:
            seen[persona.slug] = len(result.personas)
            result.personas.append(persona)
        else:
            result.personas[index] = persona  # user overrides a builtin of the same slug


def load_all() -> LoadResult:
    """Every persona: 16 builtins, then the user's own overriding by slug.
    Never raises."""
    result = LoadResult()
    seen: Dict[str, int] = {}
    _load_builtins(result, seen)
    try:
        _load_user(result, seen)
    except Exception as exc:  # noqa: BLE001 - a broken user store costs nothing else
        logger.debug("personas: user store unreadable: %s", exc)
    return result


def list_personas() -> List[Dict[str, Any]]:
    return [p.to_dict() for p in load_all().personas]


def get_persona(slug: Any) -> Optional[Persona]:
    from services.memory.skill_format import slugify
    key = slugify(str(slug or ""), fallback="")
    if not key:
        return None
    return load_all().by_slug().get(key)


def render_system_block(slug: Any) -> str:
    """The persona's identity, ready to prepend to a system prompt.

    This is the hook `src.agent_defs.resolve_task` calls when an AGENT.md
    names `persona: <slug>` — a persona is prose that goes ABOVE the agent's
    own prompt, never a replacement for it (the agent definition still owns
    every tool/path/delegation rule). Returns "" for an unknown slug rather
    than raising: a stale `persona:` reference should degrade the prompt by
    one paragraph, not take the whole agent definition down.
    """
    persona = get_persona(slug)
    if persona is None:
        return ""
    header = f"# Persona: {persona.name}"
    if persona.summary:
        header += f"\n{persona.summary}"
    return f"{header}\n\n{persona.body.strip()}"


def _clean_slug(slug: Any) -> str:
    from services.memory.skill_format import slugify
    return slugify(str(slug or ""), fallback="")


def save_user_persona(slug: Any, text: str) -> Persona:
    """Validate and write `DATA_DIR/personas/<slug>.md`. The frontmatter's own
    `slug` (if any) must agree with the path slug — otherwise a rename via
    frontmatter alone would silently orphan the old file."""
    key = _clean_slug(slug)
    if not key:
        raise PersonaError("slug: required")
    persona = parse(text, slug_hint=key, source="user", path="")
    if persona.slug != key:
        raise PersonaError(
            f"slug: frontmatter/body implies `{persona.slug}`, which does not match "
            f"the `{key}` this is being saved as — rename via the API's own slug, "
            f"not by editing the body"
        )
    root = personas_root()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{key}.md")
    persona.path = path
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(persona))
    os.replace(tmp_path, path)
    return persona


def delete_user_persona(slug: Any) -> bool:
    """Remove the USER's override, if any. Never touches a builtin — there is
    nothing under `DATA_DIR/personas/` to delete for a slug that was never
    overridden, and this returns False rather than raising."""
    key = _clean_slug(slug)
    if not key:
        return False
    path = os.path.join(personas_root(), f"{key}.md")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


__all__ = [
    "DATA_DIR", "LoadResult", "delete_user_persona", "get_persona",
    "list_personas", "load_all", "personas_root", "render_system_block",
    "save_user_persona",
]
