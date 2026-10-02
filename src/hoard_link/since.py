"""Time windows as people and assistants say them, shared by the whole family.

``resolve_since`` turns «1h», «hace 2 horas», «hoy», «esta semana», «last month», an ISO
date or epoch seconds into epoch seconds, so every tool that takes ``since`` accepts the
same words (a local model is poor at computing Unix timestamps). Anything unreadable raises
``ValueError`` whose message lists the accepted forms. ``js/hoard-link.js`` exports the same
vocabulary as ``resolveSince`` for the Node apps. Standard library only.
"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Optional, Union

SINCE_HELP = ("since accepts epoch seconds, an ISO date or time (2026-09-25, 2026-09-25T10:30), an age "
              "(30m, 2h, 3d, 2w, 1mo, «hace 2 horas», «2 hours ago»), or a word: hoy/today, ayer/yesterday, "
              "esta mañana/this morning, esta semana/this week, la semana pasada/last week, "
              "este mes/this month, el mes pasado/last month, última hora/last hour.")

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "seg": 1, "segundo": 1, "segundos": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "minuto": 60, "minutos": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "hora": 3600, "horas": 3600,
    "d": 86400, "day": 86400, "days": 86400, "dia": 86400, "dias": 86400,
    "w": 604800, "week": 604800, "weeks": 604800, "semana": 604800, "semanas": 604800,
    "mo": 2592000, "month": 2592000, "months": 2592000, "mes": 2592000, "meses": 2592000,
    "y": 31536000, "year": 31536000, "years": 31536000, "ano": 31536000, "anos": 31536000,
}
_AGE = re.compile(r"^(?:hace\s+|last\s+)?(\d+(?:[.,]\d+)?)\s*([a-z]+)(?:\s+ago)?$")
_ONE = re.compile(r"^(?:la\s+|el\s+|the\s+)?(?:ultima|ultimo|last|past)\s+([a-z]+)$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([t ][\d:.]+(z|[+-]\d{2}:?\d{2})?)?$")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text).strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text)


def _midnight(now: float) -> datetime:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_since(value: Union[str, float, int, None], now: Optional[float] = None) -> Optional[float]:
    """Epoch seconds for ``value``; None or "" stays None. Raises ValueError(SINCE_HELP)."""
    if value is None:
        return None
    now = time.time() if now is None else float(now)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _fold(value)
    if not text:
        return None
    if re.fullmatch(r"\d{9,}(\.\d+)?", text):
        return float(text)
    if _ISO.match(text):
        try:
            return datetime.fromisoformat(text.upper().replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    day = _midnight(now)
    words = {
        "hoy": day, "today": day,
        "ayer": day - timedelta(days=1), "yesterday": day - timedelta(days=1),
        "esta manana": day.replace(hour=6), "this morning": day.replace(hour=6),
        "esta semana": day - timedelta(days=day.weekday()), "this week": day - timedelta(days=day.weekday()),
        "este mes": day.replace(day=1), "this month": day.replace(day=1),
    }
    if text in words:
        return words[text].timestamp()
    if text in ("la semana pasada", "last week"):
        return now - 7 * 86400
    if text in ("el mes pasado", "last month"):
        return now - 30 * 86400
    one = _ONE.match(text)
    if one and one.group(1) in _UNITS:
        return now - _UNITS[one.group(1)]
    age = _AGE.match(text)
    if age and age.group(2) in _UNITS:
        return now - float(age.group(1).replace(",", ".")) * _UNITS[age.group(2)]
    raise ValueError(SINCE_HELP)


__all__ = ["resolve_since", "SINCE_HELP"]
