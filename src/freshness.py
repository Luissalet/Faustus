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
X", "summarize this file") — a false positive here just means an extra,
harmless web search; a false negative reproduces the original bug.
"""

from __future__ import annotations

import re
from typing import List

__all__ = ["looks_time_sensitive", "freshness_reasons"]

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
        r"\b(noticias?|qu[eé] ha pasado|[uú]ltimo|[uú]ltima|reciente|ayer|hoy|"
        r"esta semana|este mes|este a[ñn]o|ahora mismo|latest|yesterday|"
        r"this week|this month|breaking|breaking news)\b",
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
        r"\b(el tiempo|qu[eé] tiempo hace|llover[aá]|weather|forecast|"
        r"will it rain|rain tomorrow)\b",
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


def freshness_reasons(text: str) -> List[str]:
    """Return the labels of every freshness pattern that fired on ``text``.

    Empty list means the turn looks timeless (or the text is empty/blank).
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    return [label for label, pattern in _PATTERNS if pattern.search(raw)]


def looks_time_sensitive(text: str) -> bool:
    """True when ``text`` looks like it needs a live web search to answer well."""
    return bool(freshness_reasons(text))
