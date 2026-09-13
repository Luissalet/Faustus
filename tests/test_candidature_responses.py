"""tests/test_candidature_responses.py — F4.3/F4.5.

Deterministic classifier (`src/candidature_responses.py`) and job-matcher
tests against the fixtures in `tests/fixtures/candidature_mail/`, plus one
end-to-end pass wiring a fake email inbox and a fake Jobhunter
`record_employer_response` (per the CONTRATO_CONECTORES.md F4.4 contract)
against Faustus's REAL calendar routes (temp sqlite) — run twice to prove
the `external_ref` recovery path (F4.1) closes the gap the recipe's step 8
describes: an interrupted run's retry finds the event by `external_ref`
and only (re-)records the Jobhunter response, never duplicating the event.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb
from core.database import CalendarCal, CalendarEvent

from src.candidature_responses import classify, match_job

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)

FIXTURES = Path(__file__).parent / "fixtures" / "candidature_mail"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _jobs() -> list:
    return _load("jobs.json")["jobs"]


def _request(owner="tester"):
    return SimpleNamespace(state=SimpleNamespace(current_user=owner))


def _route_endpoint(router, path, method):
    full_path = f"/api/calendar{path}"
    for route in router.routes:
        if route.path == full_path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {full_path}")


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def test_classify_ack():
    result = classify(_load("ack.json"))
    assert result["kind"] == "ack"
    assert result["confidence"] == "high"
    assert result["interview_at"] is None
    assert result["tz"] is None
    assert result["evidence"]


def test_classify_rejection():
    result = classify(_load("rejection.json"))
    assert result["kind"] == "rejection"
    assert result["interview_at"] is None


def test_classify_interview_with_timezone():
    result = classify(_load("interview_tz.json"))
    assert result["kind"] == "interview"
    assert result["confidence"] == "high"
    assert result["interview_at"] == "2026-09-20T10:30:00+02:00"
    assert result["tz"] == "Europe/Madrid"


def test_classify_offer():
    msg = {
        "subject": "Oferta de empleo - Backend Engineer",
        "body": "Nos complace ofrecerte el puesto de Backend Engineer en Acme.",
        "from": "rrhh@acme.example",
        "received_at": "2026-09-10T10:00:00+02:00",
    }
    result = classify(msg)
    assert result["kind"] == "offer"
    assert result["interview_at"] is None


def test_classify_ambiguous_schedule_yields_no_event():
    """'la semana que viene' has no resolvable date/time/zone — F4.3:
    'sin fecha/hora/zona determinadas -> interview_at=None ... nunca
    inventar'. The recipe must not create a calendar event from this."""
    result = classify(_load("ambiguous_time.json"))
    assert result["kind"] == "interview"
    assert result["confidence"] == "low"
    assert result["interview_at"] is None
    assert result["tz"] is None


def test_classify_reschedule_produces_a_new_interview_time():
    first = classify(_load("reschedule_1.json"))
    second = classify(_load("reschedule_2.json"))
    assert first["kind"] == second["kind"] == "interview"
    assert first["interview_at"] == "2026-09-15T16:00:00+02:00"
    assert second["interview_at"] == "2026-09-18T11:00:00+02:00"
    assert first["interview_at"] != second["interview_at"]


def test_ack_is_never_reported_as_interview_or_offer():
    msg = _load("ack.json")
    assert "entrevista" not in msg["body"].lower()
    assert "interview" not in msg["body"].lower()
    result = classify(msg)
    assert result["kind"] not in ("interview", "offer")


def test_classify_never_inflates_confidence_without_a_resolved_moment():
    # Interview keyword present, a date but no time and no zone.
    msg = {
        "subject": "Entrevista",
        "body": "Entrevista el 20 de septiembre de 2026, ya te confirmamos la hora.",
        "from": "rrhh@acme.example",
        "received_at": "2026-09-01T00:00:00+02:00",
    }
    result = classify(msg)
    assert result["kind"] == "interview"
    assert result["confidence"] == "low"
    assert result["interview_at"] is None


# ---------------------------------------------------------------------------
# match_job()
# ---------------------------------------------------------------------------

def test_match_job_by_external_id_wins_over_everything_else():
    jobs = _jobs()
    msg = dict(_load("interview_tz.json"), external_id="jh-1002")
    result = match_job(msg, jobs)
    assert result == {"job_id": "job-beta-data", "how": "external_id", "ambiguous": []}


def test_match_job_by_thread_for_reschedule():
    jobs = _jobs()
    result = match_job(_load("reschedule_2.json"), jobs)
    assert result["job_id"] == "job-delta-platform"
    assert result["how"] == "thread"
    assert result["ambiguous"] == []


def test_match_job_by_company_and_title_fallback():
    jobs = _jobs()
    result = match_job(_load("interview_tz.json"), jobs)
    assert result["job_id"] == "job-beta-data"
    assert result["how"] == "thread"  # interview_tz.json IS in job-beta-data's thread
    # A message not in any thread still resolves via company+title text match:
    msg = {
        "subject": "Re: Data Engineer position",
        "body": "Following up on your application for Data Engineer at Beta.",
        "from": "talent@beta.example",
    }
    result2 = match_job(msg, jobs)
    assert result2 == {"job_id": "job-beta-data", "how": "company_title", "ambiguous": []}


def test_match_job_ambiguous_same_employer_never_guesses():
    jobs = _jobs()
    result = match_job(_load("ambiguous_job.json"), jobs)
    assert result["job_id"] is None
    assert result["how"] is None
    assert set(result["ambiguous"]) == {"job-acme-support-1", "job-acme-support-2"}


def test_match_job_with_no_candidates_is_not_ambiguous():
    result = match_job({"subject": "hello", "body": "", "from": "nobody@nowhere.example"}, _jobs())
    assert result == {"job_id": None, "how": None, "ambiguous": []}


# ---------------------------------------------------------------------------
# A fake Jobhunter `record_employer_response`
# (contract documented in CONTRATO_CONECTORES.md F4.4)
# ---------------------------------------------------------------------------

class _FakeJobhunter:
    _TERMINAL = {"rejected", "offer_received"}

    def __init__(self):
        self.applications: dict = {}
        self._by_external_id: dict = {}

    def record_employer_response(self, *, jobId, externalId, kind, evidence,
                                  receivedAt, interviewAt=None, timezone=None,
                                  calendarEventId=None, notes=None):
        if externalId in self._by_external_id:
            # Idempotent by externalId: a retried/duplicate message changes
            # nothing on the second call.
            return dict(self._by_external_id[externalId])
        state = self.applications.setdefault(jobId, {"status": "applied", "history": []})
        applied = False
        if kind == "rejection" and state["status"] not in self._TERMINAL:
            state["status"] = "rejected"
            applied = True
        elif kind == "offer" and state["status"] not in self._TERMINAL:
            state["status"] = "offer_received"
            applied = True
        elif kind == "interview" and interviewAt and state["status"] not in self._TERMINAL:
            state["status"] = "interview_scheduled"
            applied = True
        # ack / info_request / unknown / an interview with no resolved time
        # never change status — an ack is never an acceptance.
        state["history"].append({
            "externalId": externalId, "kind": kind, "calendarEventId": calendarEventId,
        })
        result = {"job": jobId, "applied": applied}
        self._by_external_id[externalId] = result
        return result


def test_duplicate_message_id_yields_no_changes_on_second_pass():
    jobhunter = _FakeJobhunter()
    msg = _load("rejection.json")
    result = classify(msg)
    kwargs = dict(
        jobId="job-acme-backend", externalId=msg["message_id"], kind=result["kind"],
        evidence=result["evidence"], receivedAt=msg["received_at"],
    )
    first = jobhunter.record_employer_response(**kwargs)
    second = jobhunter.record_employer_response(**kwargs)
    assert first == second
    assert len(jobhunter.applications["job-acme-backend"]["history"]) == 1


def test_ack_never_moves_a_terminal_or_non_terminal_status():
    jobhunter = _FakeJobhunter()
    ack_msg = _load("ack.json")
    ack = classify(ack_msg)
    resp = jobhunter.record_employer_response(
        jobId="job-x", externalId="m1", kind=ack["kind"], evidence=ack["evidence"],
        receivedAt=ack_msg["received_at"],
    )
    assert resp["applied"] is False
    assert jobhunter.applications["job-x"]["status"] == "applied"


def test_terminal_status_is_preserved_against_a_later_ack():
    jobhunter = _FakeJobhunter()
    rejection_msg = _load("rejection.json")
    rejection = classify(rejection_msg)
    jobhunter.record_employer_response(
        jobId="job-y", externalId="m-rejection", kind=rejection["kind"],
        evidence=rejection["evidence"], receivedAt=rejection_msg["received_at"],
    )
    assert jobhunter.applications["job-y"]["status"] == "rejected"
    ack_msg = _load("ack.json")
    ack = classify(ack_msg)
    jobhunter.record_employer_response(
        jobId="job-y", externalId="m-late-ack", kind=ack["kind"],
        evidence=ack["evidence"], receivedAt=ack_msg["received_at"],
    )
    assert jobhunter.applications["job-y"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# End-to-end: fake mail + fake Jobhunter + REAL Faustus calendar
# ---------------------------------------------------------------------------

async def _run_recipe_pass(messages, jobs, jobhunter, create_event, request):
    """Mirrors the recipe's steps (src/recipes.py `review-candidature-responses`):
    classify -> match job -> (if a confidently-timed interview) create the
    calendar event with `external_ref = jobhunter:{job_id}:{message_id}` ->
    record the employer response, attaching `calendarEventId` when one was
    created. Never marks mail read, never sends mail, never creates a
    candidature. Test-only orchestration — the recipe itself is a prompt
    procedure, not code that runs on its own (F4.4: "no se ejecuta al
    instalarse")."""
    import routes.calendar_routes as calendar_routes

    summary = {"updated": [], "interviews_added": [], "pending": []}
    for msg in messages:
        result = classify(msg)
        match = match_job(msg, jobs)
        job_id = match["job_id"]
        if job_id is None:
            summary["pending"].append(msg["message_id"])
            continue

        calendar_event_id = None
        if result["kind"] == "interview" and result["interview_at"]:
            external_ref = f"jobhunter:{job_id}:{msg['message_id']}"
            # Recovery (F4.4 step 8): a prior interrupted pass may already
            # have created this event — POST is idempotent on external_ref
            # either way, so no separate lookup-first branch is needed here.
            created = await create_event(
                request,
                calendar_routes.EventCreate(
                    summary=f"Interview: {msg['subject']}",
                    dtstart=result["interview_at"],
                    external_ref=external_ref,
                ),
            )
            calendar_event_id = created["uid"]
            summary["interviews_added"].append(calendar_event_id)

        jh_resp = jobhunter.record_employer_response(
            jobId=job_id, externalId=msg["message_id"], kind=result["kind"],
            evidence=result["evidence"], receivedAt=msg["received_at"],
            interviewAt=result["interview_at"], timezone=result["tz"],
            calendarEventId=calendar_event_id,
        )
        summary["updated"].append({
            "job_id": job_id, "kind": result["kind"], "applied": jh_resp["applied"],
        })
    return summary


@pytest.fixture
def _calendar_routes(monkeypatch):
    monkeypatch.delitem(sys.modules, "routes.calendar_routes", raising=False)
    import routes.calendar_routes as calendar_routes
    monkeypatch.setattr(calendar_routes, "SessionLocal", _TS)
    return calendar_routes


def test_end_to_end_two_passes_produce_zero_duplicates_and_the_same_summary(_calendar_routes):
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")

    owner = "e2e-tester"
    request = _request(owner)
    jobs = _jobs()
    messages = [
        _load("ack.json"),
        _load("rejection.json"),
        _load("interview_tz.json"),
        _load("ambiguous_time.json"),
        _load("reschedule_1.json"),
        _load("reschedule_2.json"),
    ]

    async def run_pass(jobhunter):
        return await _run_recipe_pass(messages, jobs, jobhunter, create_event, request)

    jobhunter = _FakeJobhunter()
    first_summary = asyncio.run(run_pass(jobhunter))
    second_summary = asyncio.run(run_pass(jobhunter))

    assert first_summary == second_summary
    # ack/rejection/ambiguous_time never produce a calendar event; only the
    # two genuinely-timed interview messages (interview_tz, reschedule_1)
    # plus the reschedule's follow-up (reschedule_2, a real reschedule with
    # its own message_id/time, not a dup) do.
    assert len(first_summary["interviews_added"]) == 3

    db = _TS()
    try:
        events = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarCal.owner == owner)
            .all()
        )
    finally:
        db.close()
    assert len(events) == 3
    assert len({e.external_ref for e in events}) == 3


def test_end_to_end_reschedule_leaves_the_first_interview_event_untouched(_calendar_routes):
    """The 15th interview and the 18th reschedule are two distinct events —
    a reschedule must never silently overwrite/delete the original."""
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")
    owner = "reschedule-tester"
    request = _request(owner)
    jobs = _jobs()
    jobhunter = _FakeJobhunter()

    asyncio.run(_run_recipe_pass([_load("reschedule_1.json")], jobs, jobhunter, create_event, request))
    asyncio.run(_run_recipe_pass([_load("reschedule_2.json")], jobs, jobhunter, create_event, request))

    db = _TS()
    try:
        events = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarCal.owner == owner)
            .order_by(CalendarEvent.dtstart)
            .all()
        )
    finally:
        db.close()
    assert [e.dtstart.isoformat() for e in events] == [
        "2026-09-15T14:00:00",  # CEST +02:00 stored as UTC (is_utc=True)
        "2026-09-18T09:00:00",
    ]
