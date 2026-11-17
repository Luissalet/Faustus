"""interface_work.py - when the request is to build or change an interface, the interface skills come with the turn.

The per-turn skill selector (`src.skills_runtime.selector`) only ranks skills the person has installed. A generated page or app screen should
come out keyboard-operable and readable by a screen reader without anyone asking, whether or not the library skill was ever installed, so
for interface work this module adds the bundled `accessible-interfaces` skill to the turn's matched skills straight from `skills/library`.
Two more bundled skills ride along on a clear signal: `ui-sdk` when the request names that library and `anime-ui-controls` when it asks for an
anime, cartoon, pixel-art or game-like look. The added bodies get their own room in the skill budget, so they never push out the skills the
selector chose, and a skill the person already has (matched, installed or not) is never added twice.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = ["BASELINE", "wants_interface", "interface_slugs", "with_interface_skills"]

BASELINE = "accessible-interfaces"

_BUILD = re.compile(
    r"\b(build|builds|building|make|create|design|generate|write|code|implement|redesign|restyle|improve|refactor|prototype|mock ?up|scaffold|"
    r"add|fix|style|crea|crear|haz|hacer|dise[nñ]a|dise[nñ]ar|genera|generar|escribe|programa|implementa|redise[nñ]a|"
    r"mejora|arregla|a[nñ]ade|a[nñ]adir|construye|construir|maqueta|monta|prepara)\b", re.IGNORECASE)
_NOUN = re.compile(
    r"\b(pages?|sites?|web ?pages?|webpages?|web ?sites?|websites?|landing(?: page)?s?|home ?pages?|web ?apps?|screens?|dashboards?|forms?|formularios?|dialogs?|modals?|"
    r"menus?|men[uú]s?|navbars?|sidebars?|tabs|pesta[nñ]as|buttons?|botones|bot[oó]n|components?|componentes?|ui|ux|user interface|interfaces?|interfaz|"
    r"front-?end|html|css|artifacts?|artefactos?|p[aá]ginas?|pantallas?|paneles?|widgets?|checkout|login|sign-?up|onboarding|toggles?|sliders?|"
    r"dropdowns?|comboboxes?|carousels?|tooltips?|toasts?)\b", re.IGNORECASE)
_FILES = re.compile(r"\.(?:html?|css|tsx|jsx|vue|svelte)\b|<(?:div|button|form|input|section|main|nav)\b", re.IGNORECASE)
_PHRASES = re.compile(r"\b(?:landing page|p[aá]gina web|sitio web|web app|user interface|front-?end|interfaz de usuario)\b", re.IGNORECASE)
_STYLE = re.compile(r"\b(anime|manga|sakuga|cartoon|dibujos animados|pixel[- ]?art|pixelado|retro game|game-?like|kawaii)\b", re.IGNORECASE)
_UI_SDK = re.compile(r"(?:@ui-sdk\b|\bui-sdk\b)", re.IGNORECASE)


def wants_interface(text: str) -> bool:
    """The message asks for an interface to be built or changed: an action verb and an interface noun, a file of the web front end, or a plain phrase."""
    text = str(text or "")
    if not text.strip():
        return False
    if _FILES.search(text) or _PHRASES.search(text):
        return True
    return bool(_BUILD.search(text) and _NOUN.search(text))


def interface_slugs(text: str) -> List[str]:
    """The bundled skills this message calls for, baseline first."""
    text = str(text or "")
    out: List[str] = []
    if _UI_SDK.search(text):
        out.extend([BASELINE, "ui-sdk"])
    elif wants_interface(text):
        out.append(BASELINE)
    if out and _STYLE.search(text):
        out.append("anime-ui-controls")
    return out


def _entry(slug: str) -> Dict[str, Any] | None:
    from src import skill_library
    return skill_library.bundled(slug)


def with_interface_skills(text: str, selected: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """`selected` plus the bundled interface skills the message calls for, and the extra level-1 token room they need.

    Returns the original list untouched with 0 extra tokens when nothing applies. A skill is skipped when `selected` already holds it by name."""
    slugs = interface_slugs(text)
    if not slugs:
        return list(selected), 0
    have = {str(s.get("name") or "") for s in selected}
    added: List[Dict[str, Any]] = []
    for slug in slugs:
        if slug in have:
            continue
        entry = _entry(slug)
        if entry is not None:
            added.append(entry)
    if not added:
        return list(selected), 0
    from src.skills_runtime.disclosure import render_level1
    extra = render_level1(added, budget_tokens=10 ** 6).tokens
    return list(selected) + added, extra
