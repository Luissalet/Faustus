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


def work_in_progress(messages: Optional[Sequence[Dict[str, Any]]]) -> bool:
    """Has this conversation already started doing things?

    A tool result or a tool call anywhere in it means the agent is mid-task.
    That matters because the whitelist recognises "sí", "ok" and "adelante",
    and in a conversation that is waiting on an approval those are not small
    talk at all -- they are the go-ahead for the next action, which is exactly
    when the model should be thinking hardest.
    """
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" or message.get("tool_calls"):
            return True
    return False


def wants_reasoning(messages: Optional[Sequence[Dict[str, Any]]], *,
                    tools: Optional[List] = None) -> bool:
    """Should this turn keep the model's reasoning on?

    Having tools available is NOT a reason to reason: agent mode is the
    default in this app, so a greeting arrives with the whole toolset attached
    and would never have qualified otherwise -- which is the case the user
    actually hits. What does disqualify a turn is the conversation having
    started to act, because then a bare "sí" is an approval and not a
    pleasantry.
    """
    if work_in_progress(messages):
        return True
    return not is_small_talk(last_user_text(messages))


def wants_tools(messages: Optional[Sequence[Dict[str, Any]]]) -> bool:
    """Should this turn still carry the whole toolset?

    Measured on this install, "hola" to the same engine, alternating so
    warm-up hits both arms equally:

        prompt cache cold   with tools 7.5 s (6,356 prompt tokens)
                            without    2.2 s (14)
        prompt cache warm   with tools 2.1 s
                            without    1.7 s

    The tool block is 25 KB of schemas and most of what the engine reads
    before it can say a word. Once it is cached it is nearly free, so the
    saving that matters is the FIRST greeting of a session -- which is also
    the one a person notices.

    (The first version of this note claimed 7.7 s -> 3.7 s, measured with
    "2+2?". That is not small talk by the rule below, so the rule never
    applied to it and the number was not this change's to claim.)

    The rule is deliberately the same one `wants_reasoning` uses, because the
    question is the same: does this turn have work in it? A conversation that
    has already called a tool keeps its tools whatever the last message says --
    that is what makes a bare "sí" after an approval card safe.
    """
    return wants_reasoning(messages)
