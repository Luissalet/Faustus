"""Does this turn need the model to think first?

Found by using the app. "Dime en dos frases qué eres" spent 37.2 seconds and
1,752 characters of reasoning, hit the token ceiling and answered with
nothing. With reasoning off the same question answered correctly in 5.5
seconds. Reasoning and the answer come out of one budget, and on a greeting
the whole budget went to the part nobody asked for.

There is already an effort profile in this codebase (`src/effort_profile.py`,
low/medium/high with reasoning budgets) but only delegated workers get one:
the turn the user is sitting and watching has never had an effort setting at
all, so it always pays for the hardest case.

This module answers the narrow question the fix needs. It is a WHITELIST, not
a classifier: it says yes only for turns it recognises as small talk, and
anything it does not recognise keeps the reasoning it has today. A turn that
thinks when it did not need to costs seconds; a turn that does not think when
it needed to costs a wrong answer, and those are not the same mistake.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

#: Above this many words a message is not small talk, whatever it says.
MAX_SMALL_TALK_WORDS = 40

#: Any of these means the turn has work in it. Deliberately broad and
#: deliberately multilingual: a false "this is small talk" is the expensive
#: mistake, so anything that smells of work disqualifies the whole turn.
_WORK_PATTERNS = (
    # files, paths, code, data
    r"\.(py|js|ts|tsx|jsx|json|md|txt|csv|yml|yaml|html|css|sql|sh|ps1|toml|ini)\b",
    r"[\\/][\w.-]+[\\/]", r"```", r"\bhttps?://",
    # asking for work, English
    # "how" and "cómo" are absent on purpose: "how are you" and "¿cómo estás?"
    # are small talk, while "how does X work" simply matches nothing in the
    # whitelist and therefore keeps its reasoning anyway. The whitelist is
    # what decides; this list only has to disqualify, never to qualify.
    r"\b(fix|debug|implement|refactor|write|build|create|add|remove|delete|"
    r"update|change|edit|review|analyz|analys|compare|explain|why|"
    r"design|plan|test|check|search|find|read|run|install|deploy|migrat|"
    r"optimi|translate|summari|calculate|convert|generate)\w*\b",
    # asking for work, Spanish
    r"\b(arregla|corrige|depura|implementa|refactoriza|escribe|construye|"
    r"crea|añade|anade|quita|borra|elimina|actualiza|cambia|edita|revisa|"
    r"analiza|compara|explica|por\s?qué|porque|diseña|"
    r"planifica|prueba|comprueba|busca|encuentra|lee|ejecuta|instala|"
    r"despliega|optimiza|traduce|resume|calcula|convierte|genera)\w*\b",
    # numbers with units, versions, ids -- something to be precise about
    r"\d+\s*(gb|mb|kb|ms|s|min|h|%|px|tokens?)\b", r"\bv?\d+\.\d+",
)
_WORK_RE = re.compile("|".join(_WORK_PATTERNS), re.IGNORECASE)

#: Recognised small talk. A message must match one of these AND nothing in
#: `_WORK_RE` to be treated as small talk.
_SMALL_TALK_PATTERNS = (
    r"^\s*(hola|hey|hi|hello|buenas|buenos días|buenas tardes|"
    r"buenas noches|good morning|good evening)\b",
    r"\b(gracias|thanks|thank you|vale|ok|okay|perfecto|genial|entendido|"
    r"de acuerdo|got it|sounds good)\b",
    r"\b(qué eres|que eres|who are you|what are you|quién eres|quien eres|"
    r"cómo te llamas|como te llamas|what can you do|qué puedes hacer|"
    r"que puedes hacer|preséntate|preséntate|introduce yourself)\b",
    r"\b(cómo estás|como estas|how are you|qué tal|que tal)\b",
    r"^\s*(sí|si|no|yes|nope|yep|claro|adelante|sigue|continue|go ahead)\s*[.!]?\s*$",
)
_SMALL_TALK_RE = re.compile("|".join(_SMALL_TALK_PATTERNS), re.IGNORECASE)


def last_user_text(messages: Optional[Sequence[Dict[str, Any]]]) -> str:
    """The most recent user message as plain text, or "".

    Blocks this codebase injects into the user role (the date line, the reply
    language line, context blocks) carry `_agent_injected`; they are skipped
    for the same reason `src/reply_language.py` skips them -- they are ours,
    not something the person typed.
    """
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user" or message.get("_agent_injected"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = [str(p.get("text", "")) for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(parts)
        text = str(content or "").strip()
        if text:
            return text
    return ""


def is_small_talk(text: str) -> bool:
    """Is this a turn that plainly needs no reasoning?

    Yes only when it looks like small talk and carries no sign of work. Short
    is not enough on its own: "fix the tests" is three words and needs every
    bit of thinking the model has.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if len(stripped.split()) > MAX_SMALL_TALK_WORDS:
        return False
    if _WORK_RE.search(stripped):
        return False
    return bool(_SMALL_TALK_RE.search(stripped))


def wants_reasoning(messages: Optional[Sequence[Dict[str, Any]]], *,
                    tools: Optional[List] = None) -> bool:
    """Should this turn keep the model's reasoning on?

    Tools keep it: a turn that can act has consequences worth thinking about,
    and the small-talk whitelist was never written with tool use in mind.
    """
    if tools:
        return True
    return not is_small_talk(last_user_text(messages))
