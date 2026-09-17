"""candidature_responses.py — F4.3: deterministic classifier for employer
replies to job applications, and evidence-based matching of a message to
the job it responds to.

Both functions are pure and deterministic: pattern matching + a small
regex date/time/timezone parser, never an LLM call and never anything that
executes text found in the message. The message body is DATA — every
string here is read, matched against, and quoted back (truncated) as
`evidence`; nothing in it is ever interpreted as an instruction. See
`docs/api/candidature_recipe.md` for how the "Revisar respuestas de
candidaturas" recipe (`src/recipes.py`) uses this module.

`dateparser` is NOT a dependency (not in requirements.txt, and the
CONTRATO_CONECTORES.md F4 instructions are explicit: do not add it) — dates
are resolved with a small ES/EN regex parser plus an explicit timezone
requirement instead. Anything the parser cannot resolve with confidence
(date AND time AND an explicit zone) yields `interview_at=None` — this
module never invents a moment it wasn't told.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, TypedDict


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

# Order matters in classify(): rejection and offer are checked first because
# both a rejection and an offer email routinely OPEN with an acknowledgement-
# shaped sentence ("Gracias por tu interés en el puesto... lamentamos
# informarte...") — checking ack first would misclassify them. A concrete,
# dated interview proposal is checked next (before the plain ack check) so a
# real invitation isn't downgraded just because it also thanks the applicant.
# Ack is deliberately never allowed to become "interview" or an implied
# acceptance: an ack email that merely mentions a future interview in passing
# ("if selected, we'll be in touch to schedule an interview") has no
# resolvable date and falls through to the ack check below the interview
# check, not the other way around.

_ACK_PATTERNS = [
    r"hemos recibido (tu|su) candidatura",
    r"hemos recibido tu solicitud",
    r"gracias por (tu|su) candidatura",
    r"gracias por (tu|su) inter[eé]s",
    r"gracias por aplicar",
    r"acuse de recibo",
    r"thank you for applying",
    r"thank you for your application",
    r"thank you for your interest",
    r"application received",
    r"we('| ha)?ve received your application",
    r"your application has been received",
]

_REJECTION_PATTERNS = [
    r"no vamos a continuar",
    r"no continuaremos",
    r"hemos decidido no continuar",
    r"hemos decidido avanzar con otro",
    r"hemos optado por otro candidato",
    r"no ha sido seleccionad",
    r"no resultaste seleccionad",
    r"lamentamos informarte",
    r"lamentamos comunicarte",
    r"unfortunately",
    r"not moving forward",
    r"we('| ha)?ve decided not to move forward",
    r"will not be moving forward",
    r"won'?t be moving forward",
    r"we have chosen to proceed with other candidates",
    r"not (been )?selected",
    r"decided to pursue other candidates",
    # The forms a real inbox showed on 17-09 (Bluehaven, Habito, Cleverfox…).
    r"we regret to inform",
    r"regret to (inform|let you know)",
    r"we regret that",
    r"will not be proceeding",
    r"not (to )?proceed(ing)? (further )?with your (application|candidacy)",
    r"decided not to proceed",
    r"not the right fit",
    r"other candidates whose (experience|profile|skills)",
    r"more closely (match|align)",
    r"we have decided to move forward with other",
    r"proceed with other (candidates|applicants)",
    r"no seguir adelante",
    r"no seguiremos adelante",
    r"hemos decidido no seguir",
    r"tu perfil no (se ajusta|encaja)",
    r"otros candidatos (cuyo|que)",
    r"descartad[oa]",
    r"no podemos ofrecerte",
    r"no hemos seleccionado tu",
]

_OFFER_PATTERNS = [
    r"oferta de empleo",
    r"oferta laboral",
    r"carta de oferta",
    r"nos complace ofrecerte",
    r"nos complace informarte que.*(oferta|puesto)",
    r"offer letter",
    r"job offer",
    r"we('| a)?re pleased to offer",
    r"pleased to extend (you )?an offer",
]

_INTERVIEW_PATTERNS = [
    r"entrevista",
    r"interview",
]

_INFO_REQUEST_PATTERNS = [
    r"podr[ií]as (enviarnos|facilitarnos|proporcionarnos|compartir)",
    r"necesitamos (m[aá]s )?informaci[oó]n",
    r"podr[ií]as confirmar",
    r"could you (please )?(send|provide|share|confirm)",
    r"we need (some )?(more )?information",
    r"please (send|provide|share|attach|confirm)",
]


class ClassifyResult(TypedDict):
    kind: str
    confidence: str
    interview_at: Optional[str]
    tz: Optional[str]
    evidence: str


def _search_any(patterns: List[str], lower_text: str):
    for pattern in patterns:
        m = re.search(pattern, lower_text, re.IGNORECASE)
        if m:
            return m
    return None


def _snippet(text: str, match, radius: int = 90) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    s = re.sub(r"\s+", " ", text[start:end]).strip()
    return s[:300]


def _result(kind: str, confidence: str, interview_at: Optional[str], tz: Optional[str], evidence: str) -> ClassifyResult:
    return {
        "kind": kind,
        "confidence": confidence,
        "interview_at": interview_at,
        "tz": tz,
        "evidence": (evidence or "")[:300],
    }


# -- date / time / timezone extraction (ES/EN regex; no external parser) ----

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_ES_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(_MONTHS_ES) + r")(?:\s+de\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_DATE_EN_RE = re.compile(
    r"\b(" + "|".join(_MONTHS_EN) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\b",
    re.IGNORECASE,
)
# Day-first English ("22 September 2026", "22nd of September"), the form
# European recruiters write.
_DATE_EN_DAY_FIRST_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(_MONTHS_EN) + r")(?:,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)

_TIME_AMPM_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp]\.?[Mm]\.?)\b")
_TIME_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

# A small fixed-offset table for common abbreviations. This ignores actual
# historical DST transition dates (an abbreviation always maps to the same
# offset) — a deliberate, documented approximation; an explicit IANA zone
# name (e.g. "Europe/Madrid") is resolved precisely via zoneinfo instead and
# is checked first.
_TZ_ABBR_OFFSETS = {
    "UTC": "+00:00", "GMT": "+00:00",
    "CET": "+01:00", "CEST": "+02:00",
    "WET": "+00:00", "WEST": "+01:00",
    "EET": "+02:00", "EEST": "+03:00",
    "BST": "+01:00",
    "EST": "-05:00", "EDT": "-04:00",
    "CST": "-06:00", "CDT": "-05:00",
    "MST": "-07:00", "MDT": "-06:00",
    "PST": "-08:00", "PDT": "-07:00",
}
_IANA_ZONE_RE = re.compile(r"\b([A-Z][a-zA-Z]+/[A-Za-z_]+)\b")
_TZ_ABBR_RE = re.compile(
    r"\b(" + "|".join(sorted(_TZ_ABBR_OFFSETS, key=len, reverse=True)) + r")\b"
)
_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)?\s*([+-]\d{2}):?(\d{2})\b")


def _find_date(text: str, ref_year: int):
    m = _DATE_ISO_RE.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date(y, mo, d)
            return y, mo, d
        except ValueError:
            pass
    m = _DATE_SLASH_RE.search(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date(y, mo, d)
            return y, mo, d
        except ValueError:
            pass
    m = _DATE_ES_RE.search(text, )
    if m:
        d = int(m.group(1))
        mo = _MONTHS_ES[m.group(2).lower()]
        y = int(m.group(3)) if m.group(3) else ref_year
        try:
            date(y, mo, d)
            return y, mo, d
        except ValueError:
            pass
    m = _DATE_EN_RE.search(text)
    if m:
        mo = _MONTHS_EN[m.group(1).lower()]
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else ref_year
        try:
            date(y, mo, d)
            return y, mo, d
        except ValueError:
            pass
    m = _DATE_EN_DAY_FIRST_RE.search(text)
    if m:
        d = int(m.group(1))
        mo = _MONTHS_EN[m.group(2).lower()]
        y = int(m.group(3)) if m.group(3) else ref_year
        try:
            date(y, mo, d)
            return y, mo, d
        except ValueError:
            pass
    return None


def _find_time(text: str):
    m = _TIME_AMPM_RE.search(text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ampm = m.group(3).lower().replace(".", "")
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return h, mi
    m = _TIME_24_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _find_tz(text: str, date_ymd):
    m = _IANA_ZONE_RE.search(text)
    if m:
        zone_name = m.group(1)
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                zi = ZoneInfo(zone_name)
            except ZoneInfoNotFoundError:
                zi = None
            if zi is not None:
                y, mo, d = date_ymd if date_ymd else (datetime.now().year, 1, 1)
                probe = datetime(y, mo, d, 12, 0, tzinfo=zi)
                offset = probe.utcoffset() or timedelta(0)
                total_min = int(offset.total_seconds() // 60)
                sign = "+" if total_min >= 0 else "-"
                total_min = abs(total_min)
                offset_str = f"{sign}{total_min // 60:02d}:{total_min % 60:02d}"
                return zone_name, offset_str
        except Exception:
            pass
    m = _TZ_ABBR_RE.search(text)
    if m:
        abbr = m.group(1).upper()
        return abbr, _TZ_ABBR_OFFSETS[abbr]
    m = _OFFSET_RE.search(text)
    if m:
        offset_str = f"{m.group(1)}:{m.group(2)}"
        return offset_str, offset_str
    return None


def _extract_interview_datetime(text: str, ref_year: int, default_tz: Optional[str] = None):
    """Return (iso_with_offset, tz_label) or (None, None).

    Requires ALL THREE of date, time and an explicit timezone to be
    resolvable — F4.3: "sin fecha/hora/zona determinadas -> interview_at=None
    ... nunca inventar". A day with no time, or a time with no stated zone,
    is exactly the ambiguous case this must refuse to guess at.

    `default_tz` (an IANA name, e.g. the user's own zone) is the one
    documented relaxation: a mail that states a date and a time but no zone
    is resolved in that zone and the label comes back as
    ``"<zone> (assumed)"`` so the caller can say so. Never applied by
    default.
    """
    date_ymd = _find_date(text, ref_year)
    time_hm = _find_time(text)
    tz_info = _find_tz(text, date_ymd)
    if date_ymd and time_hm and not tz_info and default_tz:
        assumed = _find_tz(f"{default_tz} ", date_ymd)
        if assumed:
            tz_info = (f"{assumed[0]} (assumed)", assumed[1])
    if not (date_ymd and time_hm and tz_info):
        return None, None
    y, mo, d = date_ymd
    h, mi = time_hm
    tz_label, offset_str = tz_info
    iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:00{offset_str}"
    return iso, tz_label


def _ref_year(message: Dict[str, Any]) -> int:
    raw = str(message.get("received_at") or "").strip()
    if raw:
        try:
            s2 = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            return datetime.fromisoformat(s2).year
        except Exception:
            pass
    return datetime.now().year


def classify(message: Dict[str, Any], default_tz: Optional[str] = None) -> ClassifyResult:
    """Classify one employer reply. See module docstring: deterministic,
    pattern-based, never executes the message body as instructions.

    ``message``: ``{"subject": str, "body": str, "from": str,
    "received_at": iso8601 str}`` (extra keys are ignored).
    """
    subject = str(message.get("subject") or "")
    body = str(message.get("body") or "")
    text = f"{subject}\n{body}"
    lower = text.lower()
    ref_year = _ref_year(message)

    rejection_m = _search_any(_REJECTION_PATTERNS, lower)
    if rejection_m:
        return _result("rejection", "high", None, None, _snippet(text, rejection_m))

    offer_m = _search_any(_OFFER_PATTERNS, lower)
    if offer_m:
        return _result("offer", "high", None, None, _snippet(text, offer_m))

    interview_m = _search_any(_INTERVIEW_PATTERNS, lower)
    if interview_m:
        iso, tz = _extract_interview_datetime(text, ref_year, default_tz)
        if iso and tz:
            return _result("interview", "high", iso, tz, _snippet(text, interview_m))
        # A date/time/zone could not all be resolved. If this also reads as
        # a plain acknowledgement (interview mentioned only in passing), an
        # ack must never be reported as an interview — see module docstring.
        ack_m = _search_any(_ACK_PATTERNS, lower)
        if ack_m:
            return _result("ack", "high", None, None, _snippet(text, ack_m))
        return _result("interview", "low", None, None, _snippet(text, interview_m))

    ack_m = _search_any(_ACK_PATTERNS, lower)
    if ack_m:
        return _result("ack", "high", None, None, _snippet(text, ack_m))

    info_m = _search_any(_INFO_REQUEST_PATTERNS, lower)
    if info_m:
        return _result("info_request", "high", None, None, _snippet(text, info_m))

    fallback_evidence = re.sub(r"\s+", " ", text).strip()[:300]
    return _result("unknown", "low", None, None, fallback_evidence)


# ---------------------------------------------------------------------------
# match_job()
# ---------------------------------------------------------------------------

class MatchResult(TypedDict):
    job_id: Optional[str]
    how: Optional[str]
    ambiguous: List[str]


def _message_thread_ids(message: Dict[str, Any]) -> set:
    message_id = str(message.get("message_id") or "").strip()
    in_reply_to = str(message.get("in_reply_to") or "").strip()
    references = str(message.get("references") or "").strip()
    ids = set()
    if message_id:
        ids.add(message_id)
    if in_reply_to:
        ids.add(in_reply_to)
    for token in references.split():
        token = token.strip()
        if token:
            ids.add(token)
    return ids


def match_job(message: Dict[str, Any], jobs: List[Dict[str, Any]]) -> MatchResult:
    """Match a message to the job application it responds to.

    ``jobs``: list of ``{"job_id", "company", "title", "external_id"?,
    "url"?, "thread_message_ids"?}`` (from Jobhunter's ``list_jobs``/
    ``get_application``, as fixtured in tests — this module never calls out
    to a network tool itself).

    Precedence: identifiers first (external_id / URL / thread membership —
    F4.3), each unambiguous by construction (an id either belongs to one job
    or it doesn't). Only when none of those resolve does it fall back to
    company+title text matching — and if that yields two or more candidates
    from the SAME employer, it is reported ``ambiguous`` with ``job_id=None``
    rather than guessing; the caller must never update "another application
    from the same employer" on a guess.
    """
    if not jobs:
        return {"job_id": None, "how": None, "ambiguous": []}

    external_id = str(message.get("external_id") or "").strip()
    body_and_subject = f"{message.get('subject', '')}\n{message.get('body', '')}"
    thread_ids = _message_thread_ids(message)

    if external_id:
        for job in jobs:
            jid = str(job.get("external_id") or "").strip()
            if jid and jid == external_id:
                return {"job_id": job.get("job_id"), "how": "external_id", "ambiguous": []}

    for job in jobs:
        url = str(job.get("url") or "").strip()
        if url and url in body_and_subject:
            return {"job_id": job.get("job_id"), "how": "url", "ambiguous": []}

    if thread_ids:
        for job in jobs:
            job_thread_ids = set(str(x).strip() for x in (job.get("thread_message_ids") or []) if x)
            if job_thread_ids & thread_ids:
                return {"job_id": job.get("job_id"), "how": "thread", "ambiguous": []}

    from_addr = str(message.get("from") or "").lower()
    subj_lower = str(message.get("subject") or "").lower()
    haystack = f"{from_addr}\n{subj_lower}\n{body_and_subject.lower()}"

    candidates: List[str] = []
    for job in jobs:
        company = str(job.get("company") or "").strip().lower()
        title = str(job.get("title") or "").strip().lower()
        if not company or company not in haystack:
            continue
        if title and title not in haystack:
            continue
        job_id = job.get("job_id")
        if job_id is not None and job_id not in candidates:
            candidates.append(job_id)

    if len(candidates) == 1:
        return {"job_id": candidates[0], "how": "company_title", "ambiguous": []}
    if len(candidates) >= 2:
        return {"job_id": None, "how": None, "ambiguous": candidates}
    return {"job_id": None, "how": None, "ambiguous": []}
