"""Deterministic checks on a final answer before it reaches the user.

Two things a local model gets wrong in plain sight, seen live on a three-line
quiz ("si hoy es jueves 25 de septiembre de 2026, ¿qué día será el 25 de
diciembre?"):

* it names the wrong weekday for a full date ("el 25 de diciembre de 2026
  cae en domingo" — it is a Friday), which the calendar settles exactly;
* it thinks aloud in the reply ("…espera, recalculo… no: … Déjame ser
  riguroso…"), leaving its working and self-corrections as the answer.

Both are found here without a model; `agent_loop` asks for one rewrite of
the answer when either shows up. Only full dates (day, month and year) are
checked, and only when the weekday is written right next to the date, so a
sentence that merely mentions both is not taken for a claim.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, List

_ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_ES_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_EN_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ES_DAY_RE = r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
_EN_DAY_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_ES_MONTH_RE = "(" + "|".join(_ES_MONTHS) + ")"
_EN_MONTH_RE = "(" + "|".join(_EN_MONTHS) + ")"

# "25 de diciembre de 2026 cae en domingo", "el 25 de diciembre de 2026 será domingo",
# "25 de diciembre de 2026 (domingo)"
_ES_DATE_THEN_DAY = re.compile(
    r"\b(\d{1,2})\s+de\s+" + _ES_MONTH_RE + r"\s+de(?:l)?\s+(\d{4})\**\s*"
    r"(?:\(|,|:|—|-|\bes\b|\bser[aá]\b|\bfue\b|\bcae(?:r[aá])?\s+en\b|\bca[yí]o\s+en\b|\bera\b)\s*(?:un\s+|en\s+)?\**"
    + _ES_DAY_RE + r"\b",
    re.IGNORECASE,
)
# "domingo 25 de diciembre de 2026", "domingo, 25 de diciembre de 2026"
_ES_DAY_THEN_DATE = re.compile(
    r"\b" + _ES_DAY_RE + r"\**\s*[,(]?\s*(?:el\s+)?(\d{1,2})\s+de\s+" + _ES_MONTH_RE + r"\s+de(?:l)?\s+(\d{4})\b",
    re.IGNORECASE,
)
# "December 25, 2026 is a Sunday", "December 25, 2026 (Sunday)"
_EN_DATE_THEN_DAY = re.compile(
    r"\b" + _EN_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\**\s*"
    r"(?:\(|,|:|—|-|\bis\b|\bwill\s+be\b|\bwas\b|\bfalls\s+on\b|\bfell\s+on\b)\s*(?:a\s+|on\s+)?\**"
    + _EN_DAY_RE + r"\b",
    re.IGNORECASE,
)
# "Sunday, December 25, 2026", "Sunday 25 December 2026"
_EN_DAY_THEN_DATE = re.compile(
    r"\b" + _EN_DAY_RE + r"\**\s*[,(]?\s*(?:" + _EN_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)?\s+"
    + _EN_MONTH_RE + r"),?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _fold_day(name: str) -> str:
    return name.lower().replace("é", "e").replace("á", "a")


def _check(out: List[Dict[str, str]], day: str, month: int, dnum: str, year: str, lang: str, span: str) -> None:
    try:
        date = _dt.date(int(year), month, int(dnum))
    except (TypeError, ValueError):
        return
    names = _ES_DAYS if lang == "es" else _EN_DAYS
    real = names[date.weekday()]
    if _fold_day(real) != _fold_day(day):
        item = {"date": date.isoformat(), "said": day.lower(), "real": real, "lang": lang, "text": span.strip()}
        if item not in out:
            out.append(item)


def weekday_mismatches(text: str) -> List[Dict[str, str]]:
    """Full dates the text pairs with a weekday the calendar contradicts."""
    body = str(text or "")
    out: List[Dict[str, str]] = []
    for m in _ES_DATE_THEN_DAY.finditer(body):
        _check(out, m.group(4), _ES_MONTHS[m.group(2).lower()], m.group(1), m.group(3), "es", m.group(0))
    for m in _ES_DAY_THEN_DATE.finditer(body):
        _check(out, m.group(1), _ES_MONTHS[m.group(3).lower()], m.group(2), m.group(4), "es", m.group(0))
    for m in _EN_DATE_THEN_DAY.finditer(body):
        _check(out, m.group(4), _EN_MONTHS[m.group(1).lower()], m.group(2), m.group(3), "en", m.group(0))
    for m in _EN_DAY_THEN_DATE.finditer(body):
        month = m.group(2) or m.group(5)
        dnum = m.group(3) or m.group(4)
        _check(out, m.group(1), _EN_MONTHS[month.lower()], dnum, m.group(6), "en", m.group(0))
    return out


# Self-corrections and working left in the visible answer.
_THINKING_ALOUD = re.compile(
    r"(?:\b(?:espera|wait)\s*[,.:!…]"
    r"|\bno,?\s+espera\b"
    r"|\brecalcul(?:o|emos|ando)\b"
    r"|\brevisemos\b"
    r"|\bd[eé]jame\s+(?:ser\s+riguros[oa]|recalcular|calcularlo|revisarlo|volver\s+a\s+calcular|pensar)"
    r"|\blet\s+me\s+(?:re-?check|recalculate|recount|redo|double[- ]check|think\s+again)"
    r"|\bactually,?\s+no\b"
    r"|(?:\.\.\.|…)\s*no\s*[:,])",
    re.IGNORECASE,
)
_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)


def thinking_aloud(text: str) -> List[str]:
    """The phrases of visible working found outside code blocks."""
    body = _CODE_BLOCK.sub(" ", str(text or ""))
    found: List[str] = []
    for m in _THINKING_ALOUD.finditer(body):
        phrase = m.group(0).strip()
        if phrase.lower() not in (f.lower() for f in found):
            found.append(phrase)
    return found


def rewrite_note(mismatches: List[Dict[str, str]], aloud: List[str]) -> str:
    """The runtime's request for one clean rewrite of the answer."""
    parts = ["[Harness check — automatic runtime message, not a new user request] "
             "Your last message is not shown to the user yet. Write the complete answer again, "
             "from the start, as the final answer only."]
    for m in mismatches:
        parts.append(
            f"The calendar says {m['date']} is a {m['real']}, but your answer says {m['said']} "
            f"(\"{m['text'][:80]}\"). Use the calendar's weekday and fix anything that depended on it."
        )
    if aloud:
        quoted = ", ".join(f'"{a}"' for a in aloud[:4])
        parts.append(
            f"It also shows your working and self-corrections ({quoted}). Give only the results: "
            "no working, no second thoughts. Check any arithmetic before stating it (use the python "
            "tool if you have it)."
        )
    return " ".join(parts)


_ASKS_WEEKDAY = re.compile(
    r"(?:qu[eé]\s+d[ií]a\s+(?:de\s+la\s+semana\s+)?(?:es|ser[aá]|fue|cae|caer[aá]|cay[oó]|era)"
    r"|what\s+day\s+(?:of\s+the\s+week\s+)?(?:is|was|will)|which\s+weekday)",
    re.IGNORECASE,
)
_ES_FULL_DATE = re.compile(r"\b(\d{1,2})\s+de\s+" + _ES_MONTH_RE + r"\s+de(?:l)?\s+(\d{4})\b", re.IGNORECASE)
_EN_FULL_DATE = re.compile(
    r"\b(?:" + _EN_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)?\s+" + _EN_MONTH_RE
    + r"),?\s+(\d{4})\b", re.IGNORECASE)


def _full_dates(text: str) -> List[_dt.date]:
    out: List[_dt.date] = []
    for m in _ES_FULL_DATE.finditer(text):
        try:
            out.append(_dt.date(int(m.group(3)), _ES_MONTHS[m.group(2).lower()], int(m.group(1))))
        except ValueError:
            pass
    for m in _EN_FULL_DATE.finditer(text):
        month = (m.group(1) or m.group(4)).lower()
        try:
            out.append(_dt.date(int(m.group(5)), _EN_MONTHS[month], int(m.group(2) or m.group(3))))
        except ValueError:
            pass
    return out


def asked_weekday_mismatch(question: str, answer: str) -> List[Dict[str, str]]:
    """The user asked which weekday ONE full date falls on and the answer
    names weekdays but never the right one ("2. Domingo." for a Friday, seen
    live: the date was only in the question, so `weekday_mismatches` had
    nothing to pair)."""
    q = str(question or "")
    a = str(answer or "")
    asked = _ASKS_WEEKDAY.search(q)
    if not asked:
        return []
    dates = list(dict.fromkeys(_full_dates(q)))
    if len(dates) > 1:
        # "si hoy es jueves 25 de septiembre de 2026, ¿qué día será el 25 de
        # diciembre de 2026?": the date asked about follows the question.
        dates = list(dict.fromkeys(_full_dates(q[asked.start():])))
    if len(dates) != 1:
        return []
    date = dates[0]
    folded = _fold_day(a)
    for names, lang in ((_ES_DAYS, "es"), (_EN_DAYS, "en")):
        said = [n for n in names if re.search(r"\b" + _fold_day(n) + r"\b", folded)]
        if not said:
            continue
        real = names[date.weekday()]
        if _fold_day(real) in (_fold_day(n) for n in said):
            return []
        return [{"date": date.isoformat(), "said": said[0], "real": real, "lang": lang,
                 "text": f"{date.isoformat()} → {said[0]}"}]
    return []
