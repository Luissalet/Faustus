"""Facts with a lease: the personal memory (``memory.json``) stops asserting what
nobody has confirmed for a while.

``"User lives in Madrid"`` is true until the day it is not, and nothing in the
flat store ever notices. This module gives the facts that are *about a moment*
a lease, by KIND:

==========  ==========  ===============================================
kind        lease       why
==========  ==========  ===============================================
location    30 days     where they are now moves
status      30 days     on holiday / unemployed / studying ... is a phase
project     90 days     what they are building changes with the quarter
job         365 days    employers change, slowly
preference  none        likes and dislikes are the person, not the week
==========  ==========  ===============================================

Anything the cheap rules below cannot place gets NO kind and NO lease and
behaves exactly as it always did -- an unknown kind is never a reason to
distrust a fact. Defaults are overridden with the ``memory_lease_days``
setting (``{"location": 14, "job": 0}``; ``0`` = no lease) and the whole
feature is the ``memory_leases_enabled`` switch.

What an expired lease does
--------------------------
It does NOT delete the fact and it does NOT hide it: the fact is injected
marked ``(as of 2026-08-01, may be outdated)``. Chosen over dropping it because
the failure of dropping is silent and permanent (a job fact quietly vanishes a
year in even though it is almost always still true, and nobody is asked), while
the failure of marking is visible and recoverable (the model hedges or asks,
and the owner renews or forgets the fact from the review list). The review
queue (:func:`review`) is where expired facts wait for a human decision.

New evidence ends the old lease
-------------------------------
Single-valued kinds (location, status, job) hold ONE value at a time. When a
newer fact of the same kind and slot arrives, the older one is *closed*
(``closed_at`` + ``superseded_by``): kept as history, never injected -- the
same "the newer closes the older's window" rule ``src/brain`` applies to
timed facts, here for the flat store. Forgetting the newer fact reopens the
older one (a closed fact whose superseder is gone is no longer closed).

Pure stdlib, no model call: the classifier is a handful of regexes over the
text and category, in English and Spanish.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

KINDS = ("location", "status", "job", "project", "preference")

#: Days of validity by kind. A kind that is absent (preference) has no lease.
DEFAULT_LEASE_DAYS: Dict[str, int] = {
    "location": 30,
    "status": 30,
    "project": 90,
    "job": 365,
}

#: Kinds that hold one value at a time: a newer fact closes the older one.
SINGLE_VALUED = frozenset({"location", "status", "job"})

#: How close to the end a lease is "about to expire" on the review list.
EXPIRING_WINDOW_DAYS = 7

DAY_S = 86400

_LEASE_FIELDS = ("kind", "lease_until", "lease_days", "confirmed_at")
_CLOSE_FIELDS = ("closed_at", "superseded_by")


# ---------------------------------------------------------------------------
# Settings (never raise: a broken settings file costs the feature, not the turn)
# ---------------------------------------------------------------------------


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_setting("memory_leases_enabled", True))


def window_days() -> int:
    try:
        return max(0, int(_setting("memory_lease_review_window_days", EXPIRING_WINDOW_DAYS)))
    except (TypeError, ValueError):
        return EXPIRING_WINDOW_DAYS


def lease_days_for(kind: str) -> Optional[int]:
    """Configured lease length in days for ``kind``; ``None`` = no lease."""
    kind = str(kind or "")
    overrides = _setting("memory_lease_days", {})
    if isinstance(overrides, Mapping) and kind in overrides:
        try:
            days = int(overrides[kind])
        except (TypeError, ValueError):
            return DEFAULT_LEASE_DAYS.get(kind)
        return days if days > 0 else None
    return DEFAULT_LEASE_DAYS.get(kind)


def settings_snapshot() -> Dict[str, Any]:
    return {
        "enabled": enabled(),
        "review_window_days": window_days(),
        "lease_days": {kind: lease_days_for(kind) for kind in KINDS},
        "single_valued": sorted(SINGLE_VALUED),
    }


# ---------------------------------------------------------------------------
# Classifier: cheap rules over the text and the category
# ---------------------------------------------------------------------------


def _fold(text: Any) -> str:
    """Accent-free copy (case kept): \"esta\" must match \"está\"."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_I = re.IGNORECASE

_LOCATION_NOW = re.compile(r"\b(?:currently|now|actualmente|ahora)\s+(?:in|at|en)\b", _I)
_LOCATION = re.compile(
    r"\b(?:lives?|living|resides?|residing|based|located|stays?|staying|moved|moving|"
    r"relocated|relocating)\s+(?:in|to|at|near)\b"
    r"|\b(?:vive|viven|vivo|vivimos|reside|resido|residimos)\s+(?:actualmente\s+)?(?:en|por|cerca\s+de)\b"
    r"|\bse\s+(?:ha\s+)?mud(?:o|ado)\s+a\b"
    r"|\bme\s+mude\s+a\b"
    r"|\b(?:esta|estoy|estamos)\s+(?:viviendo|instalad[oa]s?|de\s+viaje)\s+en\b",
    _I,
)
_JOB = re.compile(
    r"\b(?:works?|working|employed)\s+(?:at|for|as)\b"
    r"|\bjob\s+(?:is|title)\b"
    r"|\b(?:trabaja|trabajan|trabajo|trabajamos|trabajando)\s+(?:como|para|de)\b"
    r"|\b(?:trabaja|trabajan|trabajo|trabajamos|trabajando)\s+en\b"
    r"(?!\s+(?:el|un|una|la|mi|su)\s+(?:proyecto|app|aplicacion|tesis|novela|libro|idea|ficha|proceso))"
    r"|\bempleado\s+(?:en|de|como)\b",
    _I,
)
_PROJECT = re.compile(
    r"\b(?:working|works)\s+on\b"
    r"|\b(?:is|am|are)\s+(?:currently\s+)?(?:building|developing|writing|preparing)\b"
    r"|\bstudying\s+for\b"
    r"|\b(?:trabaja|trabajo)\s+en\s+(?:el|un|una|la|mi|su)\s+(?:proyecto|app|aplicacion|tesis|novela|libro|idea)\b"
    r"|\b(?:esta|estoy|estamos)\s+(?:trabajando\s+en|desarrollando|escribiendo|preparando|construyendo)\b",
    _I,
)
_STATUS = re.compile(
    r"\b(?:currently|right\s+now|at\s+the\s+moment|actualmente|ahora\s+mismo|en\s+este\s+momento)\b"
    r"|\b(?:is|am|are)\s+(?:currently\s+)?(?:on\s+(?:vacation|holiday|leave|sick\s+leave|parental\s+leave|a\s+trip)|"
    r"unemployed|job[- ]hunting|looking\s+for\s+(?:a\s+)?(?:job|work)|traveling|travelling|studying|sick|ill)\b"
    r"|\b(?:esta|estoy|estamos)\s+(?:de\s+(?:vacaciones|viaje|baja)|en\s+paro|desemplead[oa]s?|"
    r"buscando\s+(?:trabajo|empleo)|enferm[oa]s?|estudiando)\b"
    r"|\ben\s+paro\b",
    _I,
)
_PREFERENCE = re.compile(
    r"\b(?:prefers?|likes?|dislikes?|loves?|hates?|enjoys?|favou?rite|prefiere[n]?|"
    r"le\s+gusta[n]?|no\s+le\s+gusta[n]?|odia|disfruta|favorit[oa])\b",
    _I,
)

#: Not about the user: \"Ana lives in Paris\" must not renew or close the user's own place.
_USER_SUBJECTS = frozenset({"user", "the user", "el usuario", "la usuaria", "usuario", "usuaria", "i", "yo"})
_THIRD_SUBJECT = re.compile(
    r"^\s*([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,2})\s+"
    r"(?:lives?|vive[n]?|works?|trabaja[n]?|is|are|es|esta[n]?|moved|se\s+mudo|resides?|reside[n]?|studies|estudia)\b"
)
_RELATIVE = re.compile(
    r"\b(?:my|mi|mis)\s+(?:brother|sister|mother|father|mom|dad|wife|husband|partner|boyfriend|girlfriend|"
    r"friend|boss|son|daughter|cousin|uncle|aunt|colleague|coworker|neighbou?r|"
    r"hermano|hermana|madre|padre|mujer|marido|esposa|esposo|pareja|novio|novia|amigo|amiga|jefe|jefa|"
    r"hijo|hija|primo|prima|tio|tia|companero|companera|vecino|vecina)\b"
    r"|\b(?:his|her|their|hers)\b|^\s*(?:she|he|they|ella)\b",
    _I,
)


def _about_someone_else(folded: str) -> bool:
    if _RELATIVE.search(folded):
        return True
    match = _THIRD_SUBJECT.match(folded)
    return bool(match) and match.group(1).strip().lower() not in _USER_SUBJECTS


def classify_kind(text: Any, category: Any = "") -> str:
    """The kind of a fact (``KINDS``), or ``""`` when the rules cannot place it.

    A preference verb that comes BEFORE the place/job phrase wins (\"prefers
    living in Madrid\" is a preference, \"lives in Madrid and likes tea\" is a
    location). ``contact`` entries are about other people and never get a kind.
    """
    cat = str(category or "").strip().lower()
    if cat == "contact":
        return ""
    if cat in KINDS:
        return cat
    folded = _fold(text)
    if not folded.strip() or _about_someone_else(folded):
        return ""
    found = None
    for kind, pattern in (("location", _LOCATION_NOW), ("location", _LOCATION), ("job", _JOB),
                          ("project", _PROJECT), ("status", _STATUS)):
        match = pattern.search(folded)
        if match:
            found = (kind, match.start())
            break
    pref = _PREFERENCE.search(folded)
    if pref and (found is None or pref.start() <= found[1]):
        return "preference"
    return found[0] if found else ""


def slot_of(kind: str, text: Any) -> str:
    """What a single-valued kind competes on: employer and role are two slots,
    so \"works at Acme\" and \"works as a teacher\" do not close each other."""
    if kind == "job":
        if re.search(r"\b(?:works?|working|trabaja\w*|trabajo)\s+(?:as|como|de)\b|\bjob\s+title\b", _fold(text), _I):
            return "job:role"
        return "job:employer"
    return str(kind)


# ---------------------------------------------------------------------------
# Lease state
# ---------------------------------------------------------------------------


def _now_ts(now: Any = None) -> int:
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        return int((now if now.tzinfo else now.replace(tzinfo=timezone.utc)).timestamp())
    return int(now)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def evidence_ts(entry: Mapping[str, Any]) -> int:
    """When the fact was last stated or confirmed: the newer evidence wins."""
    return max(_int(entry.get("timestamp")), _int(entry.get("confirmed_at")))


def state(entry: Mapping[str, Any], now: Any = None, window: Optional[int] = None) -> Dict[str, Any]:
    """``{state, kind, days_left, lease_until, confirmed_at}``.

    ``state`` is one of ``none`` (no lease: every pre-lease fact), ``active``,
    ``expiring`` (inside the review window), ``expired`` or ``closed``.
    """
    ts = _now_ts(now)
    out: Dict[str, Any] = {
        "state": "none",
        "kind": str(entry.get("kind") or ""),
        "days_left": None,
        "lease_until": None,
        "confirmed_at": _int(entry.get("confirmed_at")) or _int(entry.get("timestamp")) or None,
    }
    if not enabled():
        return out
    if entry.get("closed_at"):
        out["state"] = "closed"
        return out
    until = _int(entry.get("lease_until"))
    if not until:
        return out
    out["lease_until"] = until
    out["days_left"] = round((until - ts) / DAY_S, 2)
    if ts >= until:
        out["state"] = "expired"
    elif ts >= until - (window_days() if window is None else window) * DAY_S:
        out["state"] = "expiring"
    else:
        out["state"] = "active"
    return out


def _stamp(value: int) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")


def marked_text(entry: Mapping[str, Any]) -> str:
    """The text with the staleness caveat the model sees."""
    text = str(entry.get("text") or "")
    since = _int(entry.get("confirmed_at")) or _int(entry.get("timestamp"))
    caveat = f"(as of {_stamp(since)}, may be outdated)" if since else "(may be outdated)"
    return text if caveat in text else f"{text} {caveat}".strip()


def prompt_view(entries: Iterable[Any], now: Any = None) -> List[Any]:
    """What the model may be told: closed facts out, expired facts marked.

    Entries without a lease come back as the very same objects. Never raises:
    a bad entry passes through untouched.
    """
    items = list(entries or [])
    if not enabled():
        return items
    out: List[Any] = []
    for entry in items:
        try:
            if isinstance(entry, dict):
                if entry.get("closed_at"):
                    continue
                if state(entry, now)["state"] == "expired":
                    marked = dict(entry)
                    marked["text"] = marked_text(entry)
                    marked["stale"] = True
                    out.append(marked)
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory leases: prompt_view skipped an entry (%s)", exc)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Writing leases
# ---------------------------------------------------------------------------


def _clear(entry: Dict[str, Any], fields: Iterable[str]) -> None:
    for key in fields:
        entry.pop(key, None)


def assign(entry: Dict[str, Any], *, kind: Optional[str] = None, lease_days: Optional[int] = None,
           now: Any = None) -> Dict[str, Any]:
    """Give a freshly written entry its kind and lease (mutates and returns it).

    ``kind``/``lease_days`` are explicit overrides (``lease_days=0`` = no
    lease); otherwise the kind is classified from the text and the lease taken
    from the settings. No kind, or the feature off, leaves the entry untouched.
    """
    if kind is not None and str(kind).strip() and str(kind).strip().lower() not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if lease_days is not None:
        try:
            lease_days = int(lease_days)
        except (TypeError, ValueError):
            raise ValueError("lease_days must be a whole number of days") from None
        if lease_days < 0:
            raise ValueError("lease_days must be 0 or more")
    if not enabled():
        return entry
    chosen = str(kind or "").strip().lower() or classify_kind(entry.get("text"), entry.get("category"))
    if not chosen:
        return entry
    ts = _now_ts(now)
    days = lease_days if lease_days is not None else lease_days_for(chosen)
    entry["kind"] = chosen
    if days:
        entry["lease_days"] = int(days)
        entry["confirmed_at"] = ts
        entry["lease_until"] = ts + int(days) * DAY_S
    return entry


def reassign(entry: Dict[str, Any], now: Any = None) -> Dict[str, Any]:
    """An edit is new evidence: re-read the kind from the new text and restart
    the lease. A fact that had a kind keeps it if the new text says nothing."""
    previous = str(entry.get("kind") or "")
    _clear(entry, _LEASE_FIELDS + _CLOSE_FIELDS)
    assign(entry, kind=None, now=now)
    if not entry.get("kind") and previous:
        assign(entry, kind=previous, now=now)
    return entry


def renew(entry: Dict[str, Any], days: Optional[int] = None, now: Any = None) -> Dict[str, Any]:
    """Confirm the fact is still true: restart its lease from now.

    Also reopens a closed fact -- confirming it makes it the newest evidence,
    so the next save closes whatever it had been replaced by.
    """
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError("days must be a whole number") from None
        if days <= 0:
            raise ValueError("days must be greater than 0")
    kind = str(entry.get("kind") or "") or classify_kind(entry.get("text"), entry.get("category"))
    resolved = days or _int(entry.get("lease_days")) or lease_days_for(kind)
    if not resolved:
        raise ValueError("this memory has no lease to renew; pass days to give it one")
    ts = _now_ts(now)
    if kind:
        entry["kind"] = kind
    entry["lease_days"] = int(resolved)
    entry["confirmed_at"] = ts
    entry["lease_until"] = ts + int(resolved) * DAY_S
    _clear(entry, _CLOSE_FIELDS)
    return entry


def close_superseded(entries: List[Any], now: Any = None) -> List[str]:
    """Apply \"new evidence ends the old lease\" over the whole store (mutates).

    Per owner and slot, only the newest open single-valued fact stays open.
    A closed fact whose superseder no longer exists is reopened first, so
    forgetting the wrong newer fact brings the older one back.
    """
    if not enabled():
        return []
    ids = {str(e.get("id")) for e in entries if isinstance(e, dict)}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("closed_at") and str(entry.get("superseded_by") or "") not in ids:
            _clear(entry, _CLOSE_FIELDS)
    groups: Dict[Any, list] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("closed_at"):
            continue
        kind = entry.get("kind")
        if kind not in SINGLE_VALUED:
            continue
        key = (str(entry.get("owner") or ""), slot_of(str(kind), entry.get("text")))
        groups.setdefault(key, []).append((evidence_ts(entry), index, entry))
    closed: List[str] = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (row[0], row[1]))
        newest = rows[-1][2]
        at = rows[-1][0] or _now_ts(now)
        for _, _, older in rows[:-1]:
            older["closed_at"] = int(at)
            older["superseded_by"] = newest.get("id")
            closed.append(str(older.get("id")))
    return closed


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def public(entry: Mapping[str, Any], now: Any = None) -> Dict[str, Any]:
    """The lease facts of one entry for the API/UI/MCP."""
    info = state(entry, now)
    return {
        "id": entry.get("id"),
        "text": entry.get("text", ""),
        "category": entry.get("category", "fact"),
        "kind": info["kind"],
        "state": info["state"],
        "days_left": info["days_left"],
        "lease_until": info["lease_until"],
        "confirmed_at": info["confirmed_at"],
        "lease_days": _int(entry.get("lease_days")) or None,
        "closed_at": _int(entry.get("closed_at")) or None,
        "superseded_by": entry.get("superseded_by") or None,
    }


def annotate(entry: Mapping[str, Any], now: Any = None) -> Dict[str, Any]:
    """Copy of the entry with a ``lease`` block, only when it has a kind."""
    out = dict(entry)
    if entry.get("kind") or entry.get("closed_at"):
        info = public(entry, now)
        out["lease"] = {k: info[k] for k in ("kind", "state", "days_left", "lease_until", "confirmed_at")}
    return out


def review(entries: Iterable[Any], now: Any = None, window: Optional[int] = None) -> Dict[str, Any]:
    """Expired and about-to-expire facts, plus the ones new evidence replaced."""
    expired: List[Dict[str, Any]] = []
    expiring: List[Dict[str, Any]] = []
    closed: List[Dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        info = state(entry, now, window)["state"]
        if info == "expired":
            expired.append(public(entry, now))
        elif info == "expiring":
            expiring.append(public(entry, now))
        elif info == "closed":
            closed.append(public(entry, now))
    expired.sort(key=lambda row: row["lease_until"] or 0)
    expiring.sort(key=lambda row: row["lease_until"] or 0)
    closed.sort(key=lambda row: -(row["closed_at"] or 0))
    return {
        "expired": expired,
        "expiring": expiring,
        "closed": closed,
        "counts": {"expired": len(expired), "expiring": len(expiring), "closed": len(closed)},
        "settings": settings_snapshot(),
    }


__all__ = [
    "KINDS", "DEFAULT_LEASE_DAYS", "SINGLE_VALUED", "EXPIRING_WINDOW_DAYS",
    "enabled", "lease_days_for", "settings_snapshot", "classify_kind", "slot_of",
    "state", "marked_text", "prompt_view", "assign", "reassign", "renew",
    "close_superseded", "public", "annotate", "review", "evidence_ts",
]
