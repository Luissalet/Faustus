"""Freshness detection: does a user turn look time-sensitive?

Other assistants search the web automatically for anything that may have
changed since their training cutoff (sports results, prices, news, current
office holders, weather, release versions...). Faustus used to wait for the
user to explicitly say "search" or "look up", which made local agent models
answer "I have no live access" instead of just calling ``web_search``.

This module is a small, pure, dependency-free heuristic used by
``src/agent_loop.py`` to (a) add the "web" domain so ``web_search`` /
``web_fetch`` get pulled into the turn's tool set, and (b) prepend a one-line
nudge telling the model to search first. It is intentionally conservative
about *not* firing on timeless questions (math, code, definitions, "explain
X", "summarize this file"). A false negative reproduces the original bug; a
false positive is not free either, because the nudge is an instruction and
sends the model to the web instead of to the tool that holds the answer --
see ``_OWN_THINGS`` below.

The patterns stay pure and synchronous. On top of them,
:func:`freshness_assessment` says whether the rule is confident, and the
async :func:`decide_freshness` (used by the chat route) asks a typed
decision (``src/typed_decision.py``: one prefill, the next-token
probabilities of "yes"/"no") ONLY for the turns the rule is unsure about,
under a hard latency budget, and falls back to the rule whenever the model
is not already loaded, too slow or not sure enough.
"""

from __future__ import annotations

import re
from typing import List

__all__ = ["looks_time_sensitive", "freshness_reasons", "freshness_assessment", "decide_freshness",
           "decision_verdict", "FRESHNESS_QUESTION"]

# Each entry: (label, compiled pattern). Patterns are matched case-
# insensitively against the raw user text. Spanish and English are mixed in
# on purpose — Faustus's userbase is bilingual and a single turn is usually
# one language, so there is no need to detect language first.
_PATTERNS: List[tuple] = [
    ("sports_result", re.compile(
        r"\b(gan[oó]|perdi[oó]|empat[oó]|resultado|marcador|clasificaci[oó]n|"
        r"partido|liga|jornada|who won|score|standings|match result|final score)\b",
        re.IGNORECASE,
    )),
    ("news_events", re.compile(
        r"\b(noticias?|qu[eé] ha pasado|breaking|breaking news|news)\b",
        re.IGNORECASE,
    )),
    ("time_word", re.compile(
        r"\b([uú]ltimo|[uú]ltima|reciente|ayer|hoy|"
        r"esta semana|este mes|este a[ñn]o|ahora mismo|latest|yesterday|"
        r"this week|this month)\b",
        re.IGNORECASE,
    )),
    ("prices_markets", re.compile(
        r"\b(precio|cu[aá]nto cuesta|cotizaci[oó]n|acciones|bitcoin|"
        r"euro[/\s]?d[oó]lar|d[oó]lar[/\s]?euro|price of|stock price|"
        r"exchange rate|market cap)\b",
        re.IGNORECASE,
    )),
    ("releases_versions", re.compile(
        r"\b(ha salido|cu[aá]ndo sale|nueva versi[oó]n|versi[oó]n actual|"
        r"[uú]ltima versi[oó]n|release date|latest version|(?:been|just) released)\b",
        re.IGNORECASE,
    )),
    ("office_status", re.compile(
        r"\bqui[eé]n es el (presidente|ceo|entrenador|director)\b|"
        r"\bsigue siendo\b|\bwho is the current\b|\bstill the (ceo|president|coach)\b",
        re.IGNORECASE,
    )),
    ("weather", re.compile(
        # Seen live: "¿Qué tiempo va a hacer mañana en Zaragoza? ¿Hace falta
        # paraguas?" matched none of the first forms, so the turn was routed
        # as a calendar question ("mañana").
        r"\b(el tiempo|qu[eé] tiempo (?:hace|har[aá]|va a hacer|hizo)|"
        r"llover[aá]|va a llover|llueve|lluvias?|paraguas|previsi[oó]n meteorol[oó]gica|"
        r"pron[oó]stico del tiempo|cu[aá]ntos grados|temperaturas? (?:m[aá]xima|m[ií]nima)|"
        r"weather|forecast|will it rain|rain tomorrow)\b",
        re.IGNORECASE,
    )),
    ("schedules", re.compile(
        r"\b(horario|a qu[eé] hora juega|cu[aá]ndo es|what time (?:is|does)|"
        r"when is)\b",
        re.IGNORECASE,
    )),
    ("availability", re.compile(
        r"\b(est[aá] abierto|hay entradas|in stock|sold out|is it open|"
        r"tickets available)\b",
        re.IGNORECASE,
    )),
    ("explicit_year", re.compile(r"\b20(2[4-9]|[3-9]\d)\b")),
    ("current_word", re.compile(
        r"\b(actual|actualmente|current(?:ly)?)\b", re.IGNORECASE,
    )),
]


# A bare time word says WHEN, not WHERE the answer lives. "¿Qué aplicaciones
# mías puedes usar ahora mismo?", "lee el último correo", "what's on my
# calendar today" all carry one, and all of them are about the person's own
# things -- the nudge that fired on them ("search the web before answering")
# pointed the model away from the tool that knew. The false positive the
# module docstring calls harmless is not: the nudge is an instruction. So the
# words that only date a question stop counting when the question is about
# the user's own stuff; the ones that name a public subject (a match, a
# price, the weather) still count whatever the phrasing.
_WEAK_LABELS = frozenset({"time_word", "explicit_year", "current_word"})
_OWN_THINGS = re.compile(
    r"\b(m[ií]os|m[ií]as|m[ií]o|m[ií]a|mis|mi|tengo|tenemos|nuestr[oa]s?|"
    r"my|mine|our|i have|we have|"
    r"correos?|emails?|mails?|inbox|calendario|agenda|calendar|"
    r"ficheros?|archivos?|carpetas?|files?|folders?|repo|repositorio|rama|"
    r"branch|commits?|notas?|notes?|tareas?|tasks?|chats?|conversaci[oó]n|"
    r"workspace|plugins?|conectores?|connectors?)\b",
    re.IGNORECASE,
)


# Arithmetic on dates is timeless even though it names "hoy" and a year.
# Seen live: "Si hoy es viernes 25 de septiembre de 2026, ¿qué día de la
# semana será el 1 de enero de 2027?" fired "time_word" + "explicit_year",
# the chat turn was escalated to a web search for "viernes", and 2k tokens of
# pages about the word "viernes" were put in front of a calendar sum. The
# user gave the premise, or the question is a weekday / day count: nothing
# on the web changes the answer.
_CALENDAR_MATH = re.compile(
    r"\b(?:si hoy es|suponiendo que hoy|if today is|assuming today|"
    r"qu[eé] d[ií]a de la semana|en qu[eé] d[ií]a (?:de la semana )?(?:cae|caer[aá]|cay[oó])|"
    r"what day of the week|which day of the week|"
    r"cu[aá]nt[oa]s (?:d[ií]as|semanas|meses)(?: (?:h[aá]biles|laborables|naturales))? "
    r"(?:hay|faltan|quedan|pasan|van|han pasado)|"
    r"how many (?:days|weeks|months|working days|business days) (?:are there|until|between|since|left)|"
    # "¿Cuántos lunes tiene octubre de 2026?" -- seen live: a web search
    # for printable calendars before counting five Mondays.
    r"cu[aá]nt[oa]s (?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bados|domingos|fines de semana|"
    r"d[ií]as|semanas) (?:tiene|trae|hay en)|"
    r"how many (?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|weekends|days|weeks) "
    r"(?:are )?(?:in|does))",
    re.IGNORECASE,
)


def freshness_reasons(text: str) -> List[str]:
    """Return the labels of every freshness pattern that fired on ``text``.

    Empty list means the turn looks timeless (or the text is empty/blank).
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    hits = [label for label, pattern in _PATTERNS if pattern.search(raw)]
    if hits and all(label in _WEAK_LABELS for label in hits) and (
            _OWN_THINGS.search(raw) or _CALENDAR_MATH.search(raw)):
        return []
    return hits


def looks_time_sensitive(text: str) -> bool:
    """True when ``text`` looks like it needs a live web search to answer well."""
    return bool(freshness_reasons(text))


# ---------------------------------------------------------------------------
# How sure is the rule? (pure) — and the optional typed decision (async)
# ---------------------------------------------------------------------------
#
# The patterns above are the first pass and the fallback. They are CERTAIN
# when a label that names a public, changing subject fires (a match, a price,
# the weather, an office holder...), and when the turn is plainly not a
# question about the world (empty, code, a request to write/translate/sum
# up, the person's own things). They are UNSURE when only a bare time word
# fired ("¿cuál es la última versión estable?" is fresh; "explícame el
# último paso" is not), or when nothing fired on something that reads like a
# question about the world ("¿quién dirige ahora el club?" has no keyword).
# Only the unsure cases may consult a typed decision.

_QUESTION_START = re.compile(
    r"^\s*[¿¡]?\s*(qu[eé]|qui[eé]n(?:es)?|cu[aá]ndo|d[oó]nde|cu[aá]l(?:es)?|cu[aá]nt[oa]s?|"
    r"c[oó]mo|hay|sigue|est[aá]n?|es|son|va|van|ha|han|who|what|when|where|which|how|"
    r"is|are|was|were|does|do|did|will|has|have|can|could)\b",
    re.IGNORECASE,
)
_TIMELESS_START = re.compile(
    r"^\s*[¿¡]?\s*(expl[ií]ca(?:me)?|explain|define|traduce|translate|resume|resum[eí]|"
    r"summari[sz]e|escribe|write|reescribe|rewrite|corrige|fix|refactori[sz]a|refactor|"
    r"calcula|calculate|compute|demuestra|prove|genera|generate|crea|create|haz|make|"
    r"lista|list|ordena|sort|convierte|convert|dibuja|draw)\b",
    re.IGNORECASE,
)
_MATH_ONLY = re.compile(r"^[\s\d\.\,\+\-\*/\^\(\)=x×÷%]+\??$")
_MAX_DECISION_CHARS = 1500

FRESHNESS_QUESTION = (
    "Does answering this message well require information that changes over time "
    "or recent events (news, prices, scores or results, software versions, weather, "
    "schedules, who currently holds a role), so it should be looked up on the web?"
)


def _question_like(raw: str) -> bool:
    return "?" in raw or "¿" in raw or bool(_QUESTION_START.search(raw))


def freshness_assessment(text: str) -> dict:
    """The rule verdict and whether the rule is confident about it.

    ``{"time_sensitive": bool, "confident": bool, "reasons": [...],
    "why": str}`` — ``time_sensitive`` is exactly ``looks_time_sensitive``
    (this function never changes the rule's answer, only says how much to
    trust it)."""
    raw = str(text or "")
    reasons = freshness_reasons(raw)
    verdict = bool(reasons)
    stripped = raw.strip()
    if not stripped:
        return {"time_sensitive": False, "confident": True, "reasons": [], "why": "empty"}
    all_hits = [label for label, pattern in _PATTERNS if pattern.search(raw)]
    if any(label not in _WEAK_LABELS for label in all_hits):
        return {"time_sensitive": verdict, "confident": True, "reasons": reasons, "why": "strong_label"}
    if len(stripped) > _MAX_DECISION_CHARS or "```" in raw:
        why = "long_or_code"
    elif _OWN_THINGS.search(raw):
        why = "own_things"
    elif _MATH_ONLY.match(stripped):
        why = "math"
    elif _CALENDAR_MATH.search(raw):
        why = "calendar_math"
    elif all_hits:
        return {"time_sensitive": verdict, "confident": False, "reasons": reasons, "why": "weak_label_only"}
    elif _TIMELESS_START.search(raw):
        why = "timeless_request"
    elif _question_like(raw):
        return {"time_sensitive": verdict, "confident": False, "reasons": reasons, "why": "question_no_label"}
    else:
        why = "not_a_question"
    return {"time_sensitive": verdict, "confident": True, "reasons": reasons, "why": why}


async def decide_freshness(text: str, *, owner=None) -> dict:
    """The hot-path entry: the rule first, a typed decision only when the rule
    is unsure (see :func:`freshness_assessment`), the rule again whenever the
    decision is unavailable, unsure or turned off. Never raises.

    Returns ``{"time_sensitive", "source": "rule"|"typed_decision",
    "reasons", "why", "decision": {...} | None}``; ``decision`` is the
    audited typed decision (value, confidence, mass, method, reason, ms)
    whenever one was asked, even if its answer was not used."""
    try:
        assessment = freshness_assessment(text)
    except Exception:  # noqa: BLE001
        assessment = {"time_sensitive": looks_time_sensitive(text), "confident": True,
                      "reasons": [], "why": "error"}
    result = {"time_sensitive": bool(assessment["time_sensitive"]), "source": "rule",
              "reasons": list(assessment.get("reasons") or []), "why": assessment.get("why", ""),
              "decision": None}
    if assessment.get("confident"):
        return result
    try:
        from src import typed_decision
        if not typed_decision.enabled() or not bool(typed_decision._setting("typed_decision_freshness")):
            return result
        decisions = await typed_decision.decide(
            str(text or ""),
            [typed_decision.Field("needs_web", FRESHNESS_QUESTION, "bool")],
            owner=owner, caller="freshness",
        )
        decision = decisions.get("needs_web")
    except Exception:  # noqa: BLE001 - the rule result stands
        return result
    if decision is None:
        return result
    result["decision"] = {k: getattr(decision, k) for k in
                          ("value", "best", "confidence", "mass", "method", "reason", "ms")}
    verdict = decision_verdict(decision)
    if verdict is not None:
        result["time_sensitive"] = verdict
        result["source"] = "typed_decision"
    return result


def decision_verdict(decision) -> "bool | None":
    """What a typed decision says, or None when it must not be used: only a
    log-probability answer that already passed the confidence and mass
    thresholds (``value`` set) counts — a single parsed letter has no
    probability behind it, and an unsure one is no better than the rule."""
    if decision is None or getattr(decision, "method", "") != "logprobs":
        return None
    value = getattr(decision, "value", None)
    return (value == "yes") if value in ("yes", "no") else None
