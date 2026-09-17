"""src/candidature_review.py — the `review_candidature_mail` tool: read the
last N days of mail, find the employer replies (rejections, interviews,
offers), match each to a Jobhunter's Hoard application and — when asked —
record them there and put the interviews on the calendar.

Why one deterministic tool instead of "the model does the recipe by hand"
(`docs/recipes/review-candidature-responses.json`): two weeks of a real
inbox are 300–600 messages. A 27B local model cannot page through that,
read forty bodies, keep the Jobhunter job list in its head and get every
idempotency key right — on 17-09 that is exactly what was asked of it. So
the recipe's steps live here, in code, and the model's job is reduced to
one call and a summary. Everything the recipe promises still holds:

* nothing is marked read (`mark_seen=false` on every read), no mail is sent;
* the message body is data — it is classified by `src/candidature_responses`
  patterns, never executed as instructions;
* a calendar event is created with `external_ref = jobhunter:{job}:{msg}`
  and `record_employer_response` is idempotent by the message id, so running
  the tool twice over the same window changes nothing the second time;
* an ambiguous match (two open applications at the same employer with no
  title to tell them apart) or an interview whose date/time/zone the mail
  does not state are reported for manual review, never guessed.

The tool talks to Faustus's own `/api/email/*` and `/api/calendar/*` over
the loopback with the internal token (the same way the cookbook tools do)
and to Jobhunter's Hoard over the REST endpoints its UI uses
(`GET /api/state`, `POST /api/jobs/{id}/responses`), found through the
connector's `APP_URL`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src import candidature_responses as cr
from src.tools._common import _INTERNAL_BASE, _internal_headers

logger = logging.getLogger(__name__)

PAGE = 100
MAX_MESSAGES = 800
READ_TIMEOUT_S = 25.0
#: A message is worth reading in full only when its subject/sender hints at
#: a candidature at all; everything else (newsletters, receipts) is skipped
#: without a body fetch. Deliberately broad — the classifier decides.
_HINT_RE = re.compile(
    r"applic|candidat|postula|entrevist|interview|vacan|puesto|position|role\b|proceso de selecci|"
    r"recruit|talentia|talent|hiring|job\b|oferta|offer\b|next steps|pr[oó]ximos pasos|"
    r"your (?:cv|resume|profile)|tu (?:cv|curr[ií]culum|perfil)|thank you for (?:applying|your interest)|"
    r"gracias por (?:tu inter[eé]s|aplicar|postular|tu candidatura)|update on|actualizaci[oó]n|"
    r"unfortunately|lamentamos|regret|no continuar|descartad|selected|seleccionad|"
    r"greenhouse|lever\.co|workday|smartrecruiters|teamtailor|ashby|bamboohr|workable|recruitee|personio|"
    r"careers|jobs@|talent@|hr@|people@|noreply@.*(?:jobs|careers|talent)",
    re.I,
)
#: Mails that mention a job but are not an employer's reply to an application.
_NOISE_RE = re.compile(
    r"job alert|alertas? de empleo|nuevos puestos|new jobs|jobs posted|job matches|recommended jobs|"
    r"ofertas? que podr|complete your application|final reminder|weekly|newsletter|digest|"
    r"unsubscribe|dar(?:te)? de baja|resume needs|otp code|verification code|c[oó]digo de verificaci",
    re.I,
)


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------------------
# Faustus mail / calendar over the loopback
# ---------------------------------------------------------------------------

def _client(owner: Optional[str]) -> httpx.Client:
    return httpx.Client(base_url=_INTERNAL_BASE, headers=_internal_headers(owner), timeout=READ_TIMEOUT_S)


def list_mail(client: httpx.Client, since: datetime, until: datetime, account: Optional[str] = None,
              folder: str = "INBOX") -> List[Dict[str, Any]]:
    """Every message in [since, until] (newest first). Pages `/api/email/list`
    raw (no server-side window: that filter only tells us what survived a
    page, not where the page ended) and stops at the first message older
    than `since` or at MAX_MESSAGES."""
    out: List[Dict[str, Any]] = []
    offset = 0
    lo, hi = since.timestamp(), until.timestamp()
    while offset < MAX_MESSAGES:
        params: Dict[str, Any] = {"folder": folder, "limit": PAGE, "offset": offset}
        if account:
            params["account_id"] = account
        r = client.get("/api/email/list", params=params)
        if r.status_code != 200:
            raise ReviewError(f"mail list failed ({r.status_code}): {r.text[:200]}")
        page = r.json().get("emails") or []
        if not page:
            break
        past = False
        for m in page:
            epoch = float(m.get("date_epoch") or 0)
            if epoch and epoch < lo:
                past = True
                continue
            if epoch and epoch > hi:
                continue
            out.append(m)
        if past or len(page) < PAGE:
            break
        offset += PAGE
    return out


def read_mail(client: httpx.Client, uid: str, account: Optional[str], folder: str = "INBOX") -> Dict[str, Any]:
    params = {"folder": folder, "mark_seen": "false"}
    if account:
        params["account_id"] = account
    r = client.get(f"/api/email/read/{uid}", params=params)
    if r.status_code != 200:
        raise ReviewError(f"mail read {uid} failed ({r.status_code})")
    return r.json()


def _body_text(msg: Dict[str, Any]) -> str:
    for key in ("text", "body_text", "body", "text_body", "plain"):
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return v
    html = msg.get("html") or msg.get("body_html") or ""
    if isinstance(html, str) and html:
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def create_calendar_event(client: httpx.Client, *, summary: str, dtstart: str, description: str,
                          external_ref: str, location: str = "") -> Dict[str, Any]:
    payload = {"summary": summary, "dtstart": dtstart, "description": description,
               "location": location, "external_ref": external_ref}
    r = client.post("/api/calendar/events", json=payload)
    if r.status_code != 200:
        raise ReviewError(f"calendar create failed ({r.status_code}): {r.text[:200]}")
    return r.json()


# ---------------------------------------------------------------------------
# Jobhunter's Hoard over its REST
# ---------------------------------------------------------------------------

def jobhunter_base_url() -> Optional[str]:
    try:
        from src import connector_sidecar
        for entry in connector_sidecar.list_connectors():
            if entry.get("preset_id") == "jobhunter":
                url = entry.get("app_url") or (entry.get("values") or {}).get("APP_URL")
                if url:
                    return str(url).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[candidature-review] sidecar lookup failed: %s", exc)
    return None


def jobhunter_jobs(base: str) -> List[Dict[str, Any]]:
    r = httpx.get(base + "/api/state", timeout=15.0)
    if r.status_code != 200:
        raise ReviewError(f"Jobhunter's Hoard did not answer ({r.status_code}); is the app running?")
    jobs = r.json().get("jobs") or []
    out = []
    for j in jobs:
        out.append({
            "job_id": j.get("id"), "company": j.get("company") or "", "title": j.get("title") or "",
            "url": j.get("url") or "", "external_id": j.get("externalId") or j.get("external_id") or "",
            "status": j.get("status") or "", "thread_message_ids": j.get("threadMessageIds") or [],
            "responses": [str(x.get("externalId") or "") for x in (j.get("responses") or [])],
        })
    return out


def jobhunter_capture(base: str, *, company: str, title: str, description: str) -> Dict[str, Any]:
    """Create the application Jobhunter's Hoard did not have (its own
    `POST /api/jobs` = `capture_job`; a company+title duplicate is returned
    instead of created)."""
    payload = {"title": title[:300], "company": company[:300], "description": description[:4000],
               "source": "mail", "tags": ["from-mail"]}
    r = httpx.post(base + "/api/jobs", json=payload, timeout=15.0)
    if r.status_code != 200:
        raise ReviewError(f"capture_job failed ({r.status_code}): {r.text[:200]}")
    return r.json()


_GENERIC_SENDER = re.compile(
    r"\b(talent|team|careers?|recruit\w*|hr|people|jobs?|hiring|noreply|no-reply|notifications?|"
    r"acquisition|recruitment|do not reply|equipo|seleccion|selección|rrhh|candidat\w*)\b", re.I)
_SUBJECT_COMPANY_RES = [
    re.compile(r"(?:\bat|\bwith|\bto|\bfrom|\ben|\bcon|\bde)\s+([A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*){0,3})(?=\s*(?:[!.,:;—–|-]|\bfor\b|\bpara\b|$))"),
    re.compile(r"&\s+([A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*){0,3})\s*[—–-]"),
    re.compile(r"^([A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*){0,2})\s*[—–:|-]\s"),
]
_SUBJECT_TITLE_RES = [
    re.compile(r"(?:application|candidatura|candidature)\s*(?:for the|for|para|:)\s+(.+?)(?:\s+(?:at|en)\b|[!.,—–]|$)", re.I),
    re.compile(r"(?:for the|para el puesto de|para la posición de|puesto de|posición de|position of|role of)\s+(.+?)\s+(?:at|en|position|role|puesto)\b", re.I),
    re.compile(r"\bfor\s+(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,}\s+(.+?)(?:\s*[|—–]|$)"),
]
_TITLE_JUNK = re.compile(r"^(?:the|your|tu|mi|our|job|the position of|position of|puesto de)\s+", re.I)


def guess_company(message: Dict[str, Any]) -> str:
    """The employer's name for a reply Jobhunter has no application for:
    the subject first ("…at Bluehaven!", "& Folding Forks—"), then the
    sender's display name minus the generic words, then the sender domain."""
    subject = str(message.get("subject") or "")
    for rx in _SUBJECT_COMPANY_RES:
        m = rx.search(subject)
        if m:
            cand = m.group(1).strip(" .,!-—–")
            if cand and not _GENERIC_SENDER.fullmatch(cand) and cand.lower() not in {"your", "the", "tu", "el", "la"}:
                return cand
    sender = str(message.get("from") or "")
    name = re.sub(r"<.*?>", "", sender).strip(" \"'")
    name = _GENERIC_SENDER.sub("", name).strip(" -|,@")
    name = re.sub(r"\s{2,}", " ", name)
    if name and "@" not in name and "." not in name and len(name) > 1:
        return name
    m = re.search(r"@([\w-]+)\.[\w.]+", sender)
    if m:
        return m.group(1).capitalize()
    return ""


def guess_title(message: Dict[str, Any]) -> str:
    subject = str(message.get("subject") or "")
    for rx in _SUBJECT_TITLE_RES:
        m = rx.search(subject)
        if m:
            t = m.group(1).strip(" .,!-—–")
            for _ in range(3):
                t = _TITLE_JUNK.sub("", t).strip()
            if 2 < len(t) <= 120 and not re.match(r"^(?:to|a|at|en)\s", t, re.I):
                return t
    return ""


def jobhunter_record(base: str, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = httpx.post(f"{base}/api/jobs/{job_id}/responses", json=payload, timeout=15.0)
    if r.status_code != 200:
        raise ReviewError(f"record_employer_response failed ({r.status_code}): {r.text[:200]}")
    return r.json()


# ---------------------------------------------------------------------------
# matching (company alone when it is unambiguous)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(s\.?l\.?|s\.?a\.?|inc\.?|ltd\.?|llc|gmbh|group|technologies|technology|labs?)\b", " ", s)
    return re.sub(r"[^a-z0-9áéíóúñü]+", " ", s).strip()


def match(message: Dict[str, Any], jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`cr.match_job` first (ids, URL, thread, company+title); then the
    relaxed pass the real inbox needs: a rejection rarely repeats the job
    title, so a company that names exactly ONE application matches on the
    company alone; several → narrow by title; still several → ambiguous."""
    strict = cr.match_job(message, jobs)
    if strict.get("job_id") or strict.get("ambiguous"):
        return strict
    hay = _norm(f"{message.get('from', '')}\n{message.get('subject', '')}\n{message.get('body', '')}")
    by_company: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        comp = _norm(job.get("company") or "")
        if len(comp) < 3:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(comp) + r"(?![a-z0-9])", hay):
            by_company.setdefault(comp, []).append(job)
    if not by_company:
        return {"job_id": None, "how": None, "ambiguous": []}
    # the longest company name wins ("Folding Forks" over "Spoons")
    comp = max(by_company, key=len)
    cands = by_company[comp]
    if len(cands) == 1:
        return {"job_id": cands[0]["job_id"], "how": "company", "ambiguous": []}
    titled = [j for j in cands if _norm(j.get("title") or "") and _norm(j.get("title") or "") in hay]
    if len(titled) == 1:
        return {"job_id": titled[0]["job_id"], "how": "company_title", "ambiguous": []}
    return {"job_id": None, "how": None, "ambiguous": [j["job_id"] for j in cands]}


# ---------------------------------------------------------------------------
# the review
# ---------------------------------------------------------------------------

def _user_zone_name() -> Optional[str]:
    """The user's own IANA zone (Settings), used only for an interview mail
    that states date and time but no zone; reported as assumed."""
    try:
        from src.user_time import user_timezone
        tz = user_timezone()
        key = getattr(tz, "key", None)
        if key:
            return str(key)
        off = datetime.now(tz).utcoffset() or timedelta(0)
        mins = int(off.total_seconds() // 60)
        return f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
    except Exception:  # noqa: BLE001
        return None


def _iso_z(epoch: Optional[float], fallback: str = "") -> str:
    if epoch:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return fallback


def review(*, owner: Optional[str], days: int = 14, since: Optional[str] = None, until: Optional[str] = None,
           kinds: Optional[List[str]] = None, apply: bool = False, calendar: bool = True,
           account: Optional[str] = None, folder: str = "INBOX", max_bodies: int = 120,
           create_missing: bool = True) -> Dict[str, Any]:
    """Scan, classify, match and (with `apply`) record. Returns a report the
    model can summarise: `found` rows, `updated`, `events`, `manual`, and
    counts. Never raises for one bad message; a broken dependency (mail,
    Jobhunter) raises ReviewError with a plain reason."""
    now = datetime.now(timezone.utc)
    until_dt = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else until_dt - timedelta(days=max(1, int(days)))
    wanted = set(kinds or ["rejection", "interview", "offer"])
    base = jobhunter_base_url()
    if not base:
        raise ReviewError("no Jobhunter's Hoard connector is configured (Tools → Connectors)")
    jobs = jobhunter_jobs(base)

    default_tz = _user_zone_name()
    report: Dict[str, Any] = {
        "default_timezone": default_tz,
        "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
        "apply": bool(apply), "create_missing": bool(create_missing), "jobhunter": base, "jobs_known": len(jobs),
        "scanned": 0, "read": 0, "found": [], "updated": [], "created": [], "events": [], "manual": [],
        "skipped_kinds": {},
    }
    events_done: Dict[str, str] = {}      # external_ref -> event uid (one event per interview slot)
    with _client(owner) as client:
        listing = list_mail(client, since_dt, until_dt, account=account, folder=folder)
        report["scanned"] = len(listing)
        candidates = []
        for m in listing:
            head = f"{m.get('from') or ''} {m.get('from_address') or ''} {m.get('subject') or ''}"
            if _HINT_RE.search(head) and not _NOISE_RE.search(head):
                candidates.append(m)
        candidates = candidates[:max_bodies]
        for m in candidates:
            uid = str(m.get("uid") or "")
            try:
                full = read_mail(client, uid, account, folder)
            except ReviewError as exc:
                report["manual"].append({"uid": uid, "subject": m.get("subject"), "reason": str(exc)})
                continue
            report["read"] += 1
            message = {
                "subject": full.get("subject") or m.get("subject") or "",
                "body": _body_text(full)[:20000],
                "from": f"{full.get('from_name') or ''} <{full.get('from_address') or ''}>".strip() or m.get("from") or "",
                "received_at": _iso_z(m.get("date_epoch"), str(full.get("date") or "")),
                "message_id": full.get("message_id") or "",
                "in_reply_to": full.get("in_reply_to") or "",
                "references": full.get("references") or "",
            }
            if _NOISE_RE.search(message["subject"]):
                continue
            verdict = cr.classify(message, default_tz=default_tz)
            kind = verdict["kind"]
            if kind not in wanted:
                report["skipped_kinds"][kind] = report["skipped_kinds"].get(kind, 0) + 1
                continue
            mt = match(message, jobs)
            row = {
                "uid": uid, "subject": message["subject"][:120], "from": message["from"][:80],
                "received_at": message["received_at"], "kind": kind, "confidence": verdict["confidence"],
                "evidence": verdict["evidence"][:200], "interview_at": verdict.get("interview_at"),
                "timezone": verdict.get("tz"), "job_id": mt.get("job_id"), "match": mt.get("how"),
                "ambiguous": mt.get("ambiguous") or [],
            }
            job = next((j for j in jobs if j["job_id"] == row["job_id"]), None)
            if job:
                row["company"], row["title"], row["job_status"] = job["company"], job["title"], job["status"]
            else:
                row["company"], row["title"] = guess_company(message), guess_title(message)
            report["found"].append(row)
            external_id = message["message_id"] or f"mail:{uid}"

            if not row["job_id"] and row["ambiguous"]:
                row["action"] = "manual"
                report["manual"].append({"uid": uid, "subject": row["subject"], "kind": kind,
                                         "company": row["company"], "interview_at": row["interview_at"],
                                         "reason": "ambiguous: two or more applications at this employer (" +
                                                   ", ".join(row["ambiguous"]) + ")"})
                continue
            if not row["job_id"]:
                if not (apply and create_missing and row["company"]):
                    row["action"] = "manual" if not apply else "would_create"
                    if not apply:
                        row["action"] = "would_create" if (create_missing and row["company"]) else "would_skip"
                    report["manual"].append({"uid": uid, "subject": row["subject"], "kind": kind,
                                             "company": row["company"], "interview_at": row["interview_at"],
                                             "reason": "no application in Jobhunter's Hoard matches this employer"
                                                       + ("" if row["company"] else " and the employer's name could not be read")})
                    continue
                # --- create the application Jobhunter did not have ---
                try:
                    title = row["title"] or f"Candidatura ({message['subject'][:60]})"
                    cap = jobhunter_capture(base, company=row["company"], title=title,
                                            description=f"Creada desde el correo «{message['subject']}» ({row['received_at']}).")
                    new_job = cap.get("job") or cap
                    job = {"job_id": new_job.get("id"), "company": new_job.get("company") or row["company"],
                           "title": new_job.get("title") or title, "url": new_job.get("url") or "",
                           "external_id": "", "status": new_job.get("status") or "", "thread_message_ids": [],
                           "responses": [str(x.get("externalId") or "") for x in (new_job.get("responses") or [])]}
                    jobs.append(job)
                    row["job_id"], row["title"], row["match"] = job["job_id"], job["title"], "created"
                    report["created"].append({"company": job["company"], "title": job["title"],
                                              "duplicate": bool(cap.get("duplicate"))})
                except ReviewError as exc:
                    row["action"] = f"error: {exc}"
                    continue

            if external_id in job.get("responses", []):
                row["action"] = "already_recorded"
                # the calendar can still be missing (an earlier run without calendar=true)
                if not (kind == "interview" and calendar and apply and row["interview_at"]):
                    continue
            elif not apply:
                row["action"] = "would_record"
                continue
            # --- apply ---
            event_id = None
            if kind == "interview" and calendar:
                if row["interview_at"]:
                    ref = f"jobhunter:{job['job_id']}:{row['interview_at']}"[:200]
                    if ref in events_done:
                        event_id = events_done[ref]
                    else:
                        try:
                            ev = create_calendar_event(
                                client, summary=f"Entrevista · {job['company']} — {job['title']}",
                                dtstart=row["interview_at"],
                                description=f"{message['subject']}\n\n{verdict['evidence'][:400]}"
                                            + ("\n\nZona horaria asumida (el correo no la indica)." if "(assumed)" in str(row["timezone"]) else ""),
                                external_ref=ref)
                            event_id = ev.get("uid")
                            events_done[ref] = str(event_id)
                            report["events"].append({"uid": event_id, "created": ev.get("created"),
                                                     "company": job["company"], "at": row["interview_at"],
                                                     "timezone": row["timezone"]})
                        except ReviewError as exc:
                            row["calendar_error"] = str(exc)
                else:
                    row["calendar"] = "no date/time stated in the mail — left for manual review"
                    report["manual"].append({"uid": uid, "subject": row["subject"], "kind": kind,
                                             "company": job["company"], "interview_at": None,
                                             "reason": "interview without a resolvable date/time (on-demand or to be scheduled)"})
            if row.get("action") == "already_recorded":
                if event_id:
                    try:
                        jobhunter_record(base, job["job_id"], {"externalId": external_id, "kind": kind,
                                                               "evidence": verdict["evidence"][:2000],
                                                               "receivedAt": row["received_at"], "calendarEventId": str(event_id)})
                    except ReviewError:
                        pass
                continue
            payload = {"externalId": external_id, "kind": kind, "evidence": verdict["evidence"][:2000],
                       "receivedAt": row["received_at"] or now.isoformat().replace("+00:00", "Z")}
            if row["interview_at"]:
                payload["interviewAt"] = row["interview_at"]
            if row["timezone"]:
                payload["timezone"] = str(row["timezone"])[:64]
            if event_id:
                payload["calendarEventId"] = str(event_id)
            try:
                res = jobhunter_record(base, job["job_id"], payload)
                row["action"] = "recorded" if res.get("applied", True) else f"skipped: {res.get('reason')}"
                new_status = (res.get("job") or {}).get("status")
                if new_status:
                    row["job_status_after"] = new_status
                job["responses"].append(external_id)
                report["updated"].append({"company": job["company"], "title": job["title"], "kind": kind,
                                          "status": new_status, "applied": res.get("applied", True)})
            except ReviewError as exc:
                row["action"] = f"error: {exc}"
    report["counts"] = {
        "found": len(report["found"]), "updated": len(report["updated"]), "created": len(report["created"]),
        "events": len(report["events"]),
        "manual": len(report["manual"]),
        "by_kind": {k: sum(1 for r in report["found"] if r["kind"] == k) for k in wanted},
    }
    return report
