"""Facts with a lease: the personal memory (``memory.json``) stops asserting what
nobody has confirmed for a while.

``"User lives in Madrid"`` is true until the day it is not, and nothing in the
flat store ever notices. This module gives the facts that are *about a moment*
a lease, by KIND:

==========  ==========  ===============================================
kind        lease       why
==========  ==========  ===============================================
location    30 days     where they are now moves
status      30 days     on holiday / unemployed / studying ... is a phase (expires only)
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
Single-valued kinds (location, job) hold ONE value at a time. When a newer
fact of the same kind and slot arrives, the older one is *closed*
(``closed_at`` + ``superseded_by``): kept as history, never injected -- the
same "the newer closes the older's window" rule ``src/brain`` applies to
timed facts, here for the flat store. Forgetting the newer fact reopens the
older one (a closed fact whose superseder is gone is no longer closed).

What may close what is deliberately narrow, because a wrong closure silently
removes a true fact from the prompt:

* only a fact whose SUBJECT is the user ("User ...", "I ...", "El usuario ...",
  or no subject at all) can get a place or a job -- "User's sister lives in
  Berlin", "Su hermana trabaja en Google" and "Luis's wife works at ..." are
  someone else's and get no kind;
* a place needs a proper noun after lives in / vive en ("lives in the moment"
  is not one) and a job needs an employer (a proper noun after works at /
  trabaja en) or a role (works as a ...); a side activity ("works at night",
  "trabaja en Faustus por las noches") and a trip ("currently in Berlin") do
  not replace the home or the employer;
* ``status`` is NOT single-valued: "on holiday", "currently learning Rust" and
  "currently drinks tea" are independent phases that only expire (their lease),
  so a status never closes another one;
* with ``memory_leases_enabled`` off, closed facts are injected again.

Facts that predate leases stay untyped until the user asks for a
:func:`backfill` (a dry run unless told to apply); nothing is rewritten on load.

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
#: ``status`` is NOT one of them: "on holiday", "currently learning Rust" and
#: "currently drinks tea" are independent phases, so a status only expires
#: (its lease) and never closes another status.
SINGLE_VALUED = frozenset({"location", "job"})

#: Longest lease accepted, in days (ten years).
MAX_LEASE_DAYS = 3650

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
        return min(days, MAX_LEASE_DAYS) if days > 0 else None
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

#: A capitalised word after the preposition ("lives in Madrid", "based in the
#: Netherlands"). Lower case is deliberately not enough: "lives in the moment",
#: "lives in constant fear" and "works at night" are not a place or an employer.
_CAP = r"(?:(?i:the|el|la|los|las)\s+)?[A-Z]"

#: Where the person LIVES. Only this can close an older place.
_RESIDENCE = re.compile(
    r"\b(?i:lives?|living|resides?|residing|based|located|moved|relocated)\s+(?i:in|to|at|near)\s+" + _CAP
    + r"|\b(?i:vive|viven|vivo|vivimos|viviendo|reside|resido|residimos)\s+(?:(?i:actualmente)\s+)?"
    + r"(?i:en|por|cerca\s+de)\s+" + _CAP
    + r"|\b(?i:se\s+(?:ha\s+)?mud(?:o|ado)|me\s+mude)\s+(?i:a)\s+" + _CAP
)
#: Where the person is for a while (a trip, a stay): leased, but it does not
#: replace where they live.
_LOCATION_NOW = re.compile(r"\b(?i:currently|now|actualmente|ahora)\s+(?i:in|at|en)\s+" + _CAP)
_STAYING = re.compile(
    r"\b(?i:stays?|staying)\s+(?i:in)\s+" + _CAP
    + r"|\b(?i:esta|estoy|estamos)\s+(?i:instalad[oa]s?|de\s+viaje)\s+(?i:en)\s+" + _CAP
)

#: A side activity is not THE job ("works at Faustus por las noches").
_PART_TIME = re.compile(
    r"\bat\s+night\b|\bby\s+night\b|\bnights?\b|\bevenings?\b|\bweekends?\b"
    r"|\bon\s+(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b|\bafter\s+(?:hours|work)\b"
    r"|\bon\s+the\s+side\b|\bas\s+a\s+hobby\b|\bspare\s+time\b|\bfree\s+time\b"
    r"|\bpor\s+las?\s+(?:noches?|mananas?|tardes?)\b|\bde\s+noche\b|\bpor\s+la\s+(?:noche|manana|tarde)\b"
    r"|\blos\s+(?:fines\s+de\s+semana|lunes|martes|miercoles|jueves|viernes|sabados|domingos)\b"
    r"|\bfines?\s+de\s+semana\b|\ba\s+ratos\b|\btiempo\s+libre\b",
    _I,
)
_NOT_ORG = r"(?!(?i:proyecto|app|aplicacion|tesis|novela|libro|idea|ficha|proceso|project|thesis|novel|book)\b)"
#: An employer: a proper noun after works at/for, trabaja en/para.
_EMPLOYER = re.compile(
    r"\b(?i:works?|working|employed)\s+(?i:at|for)\s+(?:(?i:the)\s+)?" + _NOT_ORG + r"[A-Z]"
    r"|\b(?i:trabaja|trabajan|trabajo|trabajamos|trabajando)\s+(?i:en|para)\s+(?:(?i:el|la|los|las)\s+)?" + _NOT_ORG + r"[A-Z]"
    r"|\b(?i:empleado|empleada)\s+(?i:en|de)\s+" + _NOT_ORG + r"[A-Z]"
)
#: A role: works as a <role>, trabaja como <rol>, job title is <x>.
_ROLE = re.compile(
    r"\b(?i:works?|working)\s+(?i:as)\s+(?i:an?|the)\s+[A-Za-z]"
    r"|\b(?i:trabaja\w*|trabajo)\s+(?i:como)\s+[A-Za-z]"
    r"|\bjob\s+title\s+(?i:is)\s+[A-Za-z]"
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
    r"unemployed|job[- ]hunting|looking\s+for\s+(?:a\s+)?(?:job|work)|traveling|travelling|studying|"
    r"(?:sick|ill)(?!\s+(?:of|and\s+tired|to\s+death)))\b"
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

#: Not about the user: "Ana lives in Paris" must not renew or close the user's own place.
_USER_SUBJECTS = frozenset({"user", "the user", "el usuario", "la usuaria", "usuario", "usuaria", "i", "yo"})
_THIRD_SUBJECT = re.compile(
    r"^\s*([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,2})\s+"
    r"(?:lives?|vive[n]?|works?|trabaja[n]?|is|are|es|esta[n]?|moved|se\s+mudo|resides?|reside[n]?|studies|estudia)\b"
)
_REL = (
    r"(?:brother|sister|sibling|mother|father|parent|parents|mom|dad|mum|wife|husband|spouse|partner|boyfriend|"
    r"girlfriend|fiance|fiancee|friend|boss|manager|son|daughter|kid|kids|child|children|cousin|uncle|aunt|nephew|niece|"
    r"grandmother|grandfather|grandma|grandpa|colleague|coworker|teammate|roommate|flatmate|neighbou?r|family|"
    r"in-laws?|mother-in-law|father-in-law|ex|"
    r"hermano|hermana|hermanos|madre|padre|padres|mama|papa|mujer|marido|esposa|esposo|pareja|novio|novia|amigo|amiga|"
    r"jefe|jefa|hijo|hija|hijos|primo|prima|tio|tia|sobrino|sobrina|abuelo|abuela|suegro|suegra|cunado|cunada|"
    r"companero|companera|vecino|vecina|familia|compañero|compañera)"
)
_RELATIVE = re.compile(
    r"\b(?:my|mi|mis)\s+" + _REL + r"\b"
    r"|\b(?:his|her|their|hers)\b|^\s*(?:she|he|they|ella)\b",
    _I,
)
#: "User's sister", "The user's brother", "Luis's wife": someone's relative.
_POSSESSED = re.compile(r"\b[\w-]+[\'\u2019]s?\s+" + _REL + r"\b", _I)
#: "El hermano del usuario", "the sister of the user", "La hermana de Luis".
_OF_USER = re.compile(
    r"\b" + _REL + r"\s+(?:del\s+usuari[oa]|de\s+la\s+usuaria|of\s+the\s+user)\b", _I)
_OF_NAME = re.compile(r"\b(?i:" + _REL + r")\s+de\s+[A-Z]")
#: "Su hermana vive en Berlin": a possessive with no antecedent is someone else.
_SU_REL = re.compile(r"^\W*(?:su|sus)\s+" + _REL + r"\b", _I)

#: The subject is the user: "User ...", "The user ...", "I ...", "Yo ...", "El usuario ...".
_USER_SUBJECT = re.compile(
    r"^\W*(?i:the\s+user|user|el\s+usuario|la\s+usuaria|usuario|usuaria|yo|i|we|nosotros)"
    r"(?:[\'\u2019](?:m|ve|d|ll))?(?![\w\'\u2019])"
)
#: "User's job title is ...", "User's city is ...": the user's own attributes.
_USER_ATTR = re.compile(
    r"^\W*(?i:the\s+)?(?i:user)[\'\u2019]s\s+(?i:job|role|title|occupation|profession|employer|city|home|residence|address|location|status)\b"
)
#: No subject at all ("Lives in Madrid"): a personal memory is about the user.
_SUBJECTLESS = re.compile(
    r"^\W*(?i:lives?|living|resides?|based|located|moved|works?|working|employed|is|am|are|currently|now|"
    r"vive[n]?|vivo|reside|resido|trabaja[n]?|trabajo|trabajando|esta|estoy|actualmente|ahora|"
    r"se\s+(?:ha\s+)?mud|me\s+mude|empleado)\b"
)


def _about_someone_else(folded: str) -> bool:
    if _RELATIVE.search(folded) or _POSSESSED.search(folded) or _OF_USER.search(folded):
        return True
    if _OF_NAME.search(folded) or _SU_REL.search(folded):
        return True
    match = _THIRD_SUBJECT.match(folded)
    return bool(match) and match.group(1).strip().lower() not in _USER_SUBJECTS


def _subject_is_user(folded: str) -> bool:
    """Single-valued kinds are only for facts whose SUBJECT is the user: a
    possessive ("X's ...", "... del usuario"), a name or any other subject is a
    third party and can never close the user's own place or job."""
    return bool(_USER_SUBJECT.match(folded) or _USER_ATTR.match(folded) or _SUBJECTLESS.match(folded))


def classify_kind(text: Any, category: Any = "") -> str:
    """The kind of a fact (``KINDS``), or ``\"\"`` when the rules cannot place it.

    A preference verb that comes BEFORE the place/job phrase wins (\"prefers
    living in Madrid\" is a preference, \"lives in Madrid and likes tea\" is a
    location). ``contact`` entries are about other people and never get a kind.
    Location and job need the user as the subject and a proper-noun place or
    employer (or a role); anything vaguer gets no kind rather than a wrong one.
    """
    cat = str(category or "").strip().lower()
    if cat == "contact":
        return ""
    if cat in KINDS:
        return cat
    folded = _fold(text)
    if not folded.strip() or _about_someone_else(folded):
        return ""
    user = _subject_is_user(folded)
    side = bool(_PART_TIME.search(folded))
    checks = []
    if user and not side:
        checks.append(("location", (_LOCATION_NOW, _RESIDENCE, _STAYING)))
        checks.append(("job", (_EMPLOYER, _ROLE)))
    checks.append(("project", (_PROJECT,)))
    checks.append(("status", (_STATUS,)))
    found = None
    for kind, patterns in checks:
        for pattern in patterns:
            match = pattern.search(folded)
            if match:
                found = (kind, match.start())
                break
        if found:
            break
    pref = _PREFERENCE.search(folded)
    if pref and (found is None or pref.start() <= found[1]):
        return "preference"
    return found[0] if found else ""


def slot_of(kind: str, text: Any) -> str:
    """What a single-valued kind competes on, or ``\"\"`` when this fact must
    never close another: employer and role are two slots (\"works at Acme\" and
    \"works as a teacher\" do not close each other), and only a real residence
    closes a place -- a trip, a side job or a third party's fact never does."""
    kind = str(kind)
    if kind not in SINGLE_VALUED:
        return kind
    folded = _fold(text)
    if _about_someone_else(folded) or not _subject_is_user(folded) or _PART_TIME.search(folded):
        return ""
    if kind == "location":
        return "location" if _RESIDENCE.search(folded) else ""
    if kind == "job":
        if _ROLE.search(folded):
            return "job:role"
        if _EMPLOYER.search(folded):
            return "job:employer"
    return ""

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
    """The calendar day in the user's time zone (UTC when none is known)."""
    try:
        from src.user_time import user_timezone
        zone = user_timezone()
    except Exception:  # noqa: BLE001
        zone = timezone.utc
    return datetime.fromtimestamp(value, tz=zone).strftime("%Y-%m-%d")


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
        if lease_days > MAX_LEASE_DAYS:
            raise ValueError(f"lease_days must be at most {MAX_LEASE_DAYS}")
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
        if days > MAX_LEASE_DAYS:
            raise ValueError(f"days must be at most {MAX_LEASE_DAYS}")
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

    Per owner and slot, only the newest open single-valued fact stays open. A
    fact with no slot (a third party's, a trip, a side job, a vague place) is
    never closed and never closes anything.
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
        slot = slot_of(str(kind), entry.get("text"))
        if not slot:
            continue
        key = (str(entry.get("owner") or ""), slot)
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


def _untyped(entry: Any) -> bool:
    """A fact from before leases (or one the rules could not place): no kind,
    still open, and not a contact (about someone else)."""
    return (isinstance(entry, dict) and not entry.get("kind") and not entry.get("closed_at")
            and str(entry.get("category") or "").strip().lower() != "contact")


def review(entries: Iterable[Any], now: Any = None, window: Optional[int] = None) -> Dict[str, Any]:
    """Expired and about-to-expire facts, plus the ones new evidence replaced,
    and how many older facts have no kind yet (``untyped``: ``total`` and the
    ``typable`` ones the rules could place -- see :func:`backfill`)."""
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
    untyped = [e for e in entries or [] if _untyped(e)]
    return {
        "expired": expired,
        "expiring": expiring,
        "closed": closed,
        "counts": {"expired": len(expired), "expiring": len(expiring), "closed": len(closed)},
        "untyped": {
            "total": len(untyped),
            "typable": sum(1 for e in untyped if classify_kind(e.get("text"), e.get("category"))) if enabled() else 0,
        },
        "settings": settings_snapshot(),
    }


def backfill(entries: List[Any], *, apply: bool = False, basis: str = "recorded", now: Any = None,
             only_ids: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Give the facts that predate leases a kind and a lease, ON DEMAND.

    Nothing runs on load or on its own: the default is a dry run that lists what
    WOULD change (kind, lease, the state it would land in, and which other facts
    the newly typed ones would close) and touches nothing. ``apply=True`` writes
    the kind and lease into the entries it was given (the caller saves).
    ``basis="recorded"`` counts the lease from when the fact was saved, so an old
    "User lives in Madrid" lands expired and waits for review; ``"now"`` starts
    every lease today. ``only_ids`` limits it to the caller's own facts.
    """
    if basis not in ("recorded", "now"):
        raise ValueError('basis must be "recorded" or "now"')
    ts = _now_ts(now)
    scope = None if only_ids is None else {str(i) for i in only_ids}
    items = [e for e in entries or [] if isinstance(e, dict)]
    trial = [dict(e) for e in items]
    untyped_before = sum(1 for e in items if _untyped(e) and (scope is None or str(e.get("id")) in scope))
    changes: List[Dict[str, Any]] = []
    if enabled():
        for original, copy in zip(items, trial):
            if not _untyped(copy) or (scope is not None and str(copy.get("id")) not in scope):
                continue
            kind = classify_kind(copy.get("text"), copy.get("category"))
            if not kind:
                continue
            days = lease_days_for(kind)
            copy["kind"] = kind
            if days:
                base = ts if basis == "now" else (_int(copy.get("timestamp")) or ts)
                copy["lease_days"] = int(days)
                copy["confirmed_at"] = base
                copy["lease_until"] = base + int(days) * DAY_S
            changes.append({"id": copy.get("id"), "text": copy.get("text", ""), "kind": kind, "lease_days": days,
                            "_orig": original, "_copy": copy})
    closed_ids = set(close_superseded(trial, now=ts)) if changes else set()
    rows: List[Dict[str, Any]] = []
    for change in changes:
        original, copy = change.pop("_orig"), change.pop("_copy")
        info = state(copy, ts)
        change["state"] = "closed" if copy.get("closed_at") else info["state"]
        change["days_left"] = info["days_left"]
        change["closes"] = [str(e.get("id")) for e in trial
                            if str(e.get("id")) in closed_ids and e.get("superseded_by") == copy.get("id")]
        rows.append(change)
        if apply:
            for key in ("kind",) + _LEASE_FIELDS[1:]:
                if key in copy:
                    original[key] = copy[key]
    return {
        "applied": bool(apply),
        "basis": basis,
        "changes": rows,
        "typed": len(rows),
        "left_untyped": untyped_before - len(rows),
    }


__all__ = [
    "KINDS", "DEFAULT_LEASE_DAYS", "SINGLE_VALUED", "EXPIRING_WINDOW_DAYS",
    "enabled", "lease_days_for", "settings_snapshot", "classify_kind", "slot_of",
    "state", "marked_text", "prompt_view", "assign", "reassign", "renew",
    "close_superseded", "public", "annotate", "review", "backfill", "evidence_ts", "MAX_LEASE_DAYS",
]
