"""review_candidature_mail (src/candidature_review.py): the recipe in code.
Mail, calendar and Jobhunter are faked at the function level; the
classifier and the matcher are the real ones."""
from __future__ import annotations

import json

import pytest

from src import candidature_review as cr


JOBS = [
    {"job_id": "j1", "company": "Bluehaven", "title": "AI Engineer", "url": "", "external_id": "", "status": "applied",
     "thread_message_ids": [], "responses": []},
    {"job_id": "j2", "company": "Cordera", "title": "AI Software Engineer", "url": "", "external_id": "",
     "status": "applied", "thread_message_ids": [], "responses": []},
    {"job_id": "j3", "company": "Folding Forks", "title": "Software Engineer", "url": "", "external_id": "",
     "status": "applied", "thread_message_ids": [], "responses": []},
    {"job_id": "j4", "company": "Cleverfox", "title": "ML Engineer", "url": "", "external_id": "", "status": "applied",
     "thread_message_ids": [], "responses": []},
    {"job_id": "j5", "company": "Cleverfox", "title": "Backend Engineer", "url": "", "external_id": "",
     "status": "applied", "thread_message_ids": [], "responses": []},
]

MAILS = {
    "1": {"subject": "Update on your application for the AI Engineer at Bluehaven!", "from_name": "Bluehaven Talent",
          "from_address": "jobs@bluehaven.com", "message_id": "<m1@bluehaven>", "date": "2026-09-16T09:05:00+00:00",
          "body": "Hi Luis, thank you for your interest. Unfortunately we have decided not to move forward with your application at this time."},
    "2": {"subject": "Your virtual interview at Cordera for R00123456 AI Software Engineer", "from_name": "Cordera",
          "from_address": "noreply@cordera.com", "message_id": "<m2@acc>", "date": "2026-09-16T11:01:00+00:00",
          "body": "We would like to invite you to an interview on 22 September 2026 at 10:00 (CEST). Please confirm."},
    "3": {"subject": "Thank you for applying to Cleverfox", "from_name": "Cleverfox", "from_address": "hr@cleverfox.com",
          "message_id": "<m3@sc>", "date": "2026-09-17T10:37:00+00:00",
          "body": "Thanks for applying! We have received your application and will review it."},
    "4": {"subject": "Nuevas alertas de empleo en Madrid", "from_name": "Portal", "from_address": "alerts@portal.com",
          "message_id": "<m4@p>", "date": "2026-09-17T04:56:00+00:00", "body": "50 nuevas ofertas."},
    "6": {"subject": "Regarding your application at Habito", "from_name": "Habito", "from_address": "talent@habito.io",
          "message_id": "<m6@dw>", "date": "2026-09-17T08:32:00+00:00",
          "body": "Unfortunately we will not be moving forward with your application."},
    "7": {"subject": "Interview invitation: Backend Engineer at Storeful", "from_name": "Storeful People",
          "from_address": "people@storeful.com", "message_id": "<m7@sf>", "date": "2026-09-07T10:00:00+00:00",
          "body": "We'd love to interview you. Please pick a slot in the link below."},
    "8": {"subject": "Your virtual interview at Cordera for R00123456 AI Software Engineer is in one hour!",
          "from_name": "Cordera", "from_address": "noreply@cordera.com", "message_id": "<m8@acc>",
          "date": "2026-09-22T08:00:00+00:00",
          "body": "Your 1 hour virtual interview is in one hour. Monday, September 22 at 10:00 AM CEST"},
    "9": {"subject": "Cordera would like to schedule a virtual interview with you", "from_name": "Cordera",
          "from_address": "noreply@cordera.com", "message_id": "<m9@acc>", "date": "2026-09-15T08:00:00+00:00",
          "body": "Please pick a slot. Suggested: September 19 at 9:00 AM CEST or later."},
    "5": {"subject": "Regarding your application at Cleverfox", "from_name": "Cleverfox", "from_address": "hr@cleverfox.com",
          "message_id": "<m5@sc>", "date": "2026-09-17T08:32:00+00:00",
          "body": "After careful consideration we regret to inform you that we will not be proceeding."},
}


@pytest.fixture
def fake(monkeypatch):
    recorded, events = [], []
    from datetime import datetime
    listing = [{"uid": uid, "subject": m["subject"], "from": m["from_name"], "from_address": m["from_address"],
                "date_epoch": datetime.fromisoformat(m["date"]).timestamp()} for uid, m in MAILS.items()]
    monkeypatch.setattr(cr, "_client", lambda owner: _Ctx())
    monkeypatch.setattr(cr, "list_mail", lambda client, since, until, account=None, folder="INBOX": listing)
    monkeypatch.setattr(cr, "read_mail", lambda client, uid, account, folder="INBOX": dict(MAILS[uid], uid=uid))
    monkeypatch.setattr(cr, "jobhunter_base_url", lambda: "http://127.0.0.1:5178")
    monkeypatch.setattr(cr, "jobhunter_jobs", lambda base: [dict(j, responses=list(j["responses"])) for j in JOBS])

    captured = []

    def _capture(base, company, title, description):
        captured.append((company, title))
        return {"job": {"id": f"new-{len(captured)}", "company": company, "title": title, "status": "saved"},
                "duplicate": False}

    monkeypatch.setattr(cr, "jobhunter_capture", _capture)
    cr._TEST_CAPTURED = captured

    def _record(base, job_id, payload):
        recorded.append((job_id, payload))
        status = {"rejection": "rejected", "interview": "interview", "offer": "offer"}.get(payload["kind"])
        return {"job": {"id": job_id, "status": status}, "applied": True}

    def _event(client, **kw):
        events.append(kw)
        return {"ok": True, "uid": f"ev-{len(events)}", "created": True}

    monkeypatch.setattr(cr, "jobhunter_record", _record)
    monkeypatch.setattr(cr, "create_calendar_event", _event)
    return recorded, events


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dry_run_finds_rejections_and_interviews_and_records_nothing(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=False)
    kinds = {r["uid"]: r["kind"] for r in rep["found"]}
    assert kinds["1"] == "rejection" and kinds["2"] == "interview"
    assert "3" not in kinds, "an ack is never reported"
    assert "4" not in kinds, "job alerts are noise"
    assert rep["found"][0]["action"] == "would_record"
    assert not recorded and not events
    assert rep["counts"]["found"] >= 2


def test_apply_records_in_jobhunter_and_puts_the_interview_on_the_calendar(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=True)
    by_ext = {p["externalId"]: (j, p) for j, p in recorded}
    assert by_ext["<m1@bluehaven>"][0] == "j1" and by_ext["<m1@bluehaven>"][1]["kind"] == "rejection"
    j2, acc = by_ext["<m2@acc>"]
    assert j2 == "j2" and acc["kind"] == "interview" and acc["interviewAt"] == "2026-09-22T08:00:00Z"  # UTC for zod
    assert acc["calendarEventId"] == "ev-1"
    assert events and events[0]["external_ref"] == "jobhunter:j2:2026-09-22T10:00:00+02:00"  # one slot, one event
    assert "Cordera" in events[0]["summary"]
    assert any(u["company"] == "Bluehaven" and u["status"] == "rejected" for u in rep["updated"])


def test_two_open_applications_at_the_same_employer_are_left_for_manual_review(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=True, kinds=["rejection"])
    cleverfox = next(r for r in rep["found"] if r["uid"] == "5")
    assert cleverfox["action"] == "manual" and set(cleverfox["ambiguous"]) == {"j4", "j5"}
    assert all(j != "j4" and j != "j5" for j, _ in recorded)
    assert any("ambiguous" in m["reason"] for m in rep["manual"])


def test_second_run_is_a_no_op_when_jobhunter_already_has_the_message(fake, monkeypatch):
    recorded, events = fake
    done = [dict(j, responses=(["<m1@bluehaven>"] if j["job_id"] == "j1" else [])) for j in JOBS]
    monkeypatch.setattr(cr, "jobhunter_jobs", lambda base: done)
    rep = cr.review(owner="admin", days=14, apply=True, kinds=["rejection"])
    bluehaven = next(r for r in rep["found"] if r["uid"] == "1")
    assert bluehaven["action"] == "already_recorded"
    assert not any(j == "j1" for j, _ in recorded)


def test_match_relaxes_to_the_company_when_it_names_one_application():
    msg = {"subject": "Regarding your application at Habito", "from": "Habito <talent@habito.io>",
           "body": "we will not proceed"}
    jobs = [{"job_id": "d1", "company": "Habito", "title": "AI Engineer"},
            {"job_id": "x1", "company": "Other Co", "title": "AI Engineer"}]
    assert cr.match(msg, jobs)["job_id"] == "d1"
    # company + title beats company alone when the employer has two openings
    jobs2 = jobs + [{"job_id": "d2", "company": "Habito", "title": "Backend Engineer"}]
    assert cr.match(msg, jobs2)["ambiguous"] == ["d1", "d2"]
    msg2 = dict(msg, body="we will not proceed with your Backend Engineer application")
    assert cr.match(msg2, jobs2)["job_id"] == "d2"


def test_tool_is_registered_everywhere():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS as TOOL_DESCRIPTIONS
    from src import tool_capabilities
    names = {t["function"]["name"] for t in FUNCTION_TOOL_SCHEMAS}
    assert "review_candidature_mail" in names
    assert "review_candidature_mail" in TOOL_TAGS
    assert "review_candidature_mail" in TOOL_DESCRIPTIONS
    caps = tool_capabilities.capabilities_for_tool("review_candidature_mail")
    assert caps.known and tool_capabilities.ToolEffect.WRITE_PRIVATE in caps.effects


def test_an_employer_jobhunter_does_not_know_is_created_and_recorded(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=True, kinds=["rejection", "interview"])
    assert ("Habito", "Candidatura (Regarding your application at Habito)") in cr._TEST_CAPTURED or \
        any(c == "Habito" for c, _ in cr._TEST_CAPTURED)
    habito = next(r for r in rep["found"] if r["uid"] == "6")
    assert habito["match"] == "created" and habito["action"] == "recorded"
    assert any(u["company"] == "Habito" and u["kind"] == "rejection" for u in rep["updated"])
    assert rep["counts"]["created"] >= 1
    # the interview without a date: application created, response recorded, no event, manual note
    shop = next(r for r in rep["found"] if r["uid"] == "7")
    assert shop["kind"] == "interview" and shop["action"] == "recorded" and shop["interview_at"] is None
    assert any(m["uid"] == "7" and "without a resolvable date" in m["reason"] for m in rep["manual"])
    assert not any("Storeful" in e["summary"] for e in events)


def test_create_missing_false_leaves_unknown_employers_for_manual_review(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=True, kinds=["rejection"], create_missing=False)
    assert not cr._TEST_CAPTURED
    assert any(m["uid"] == "6" and "no application" in m["reason"] for m in rep["manual"])


def test_guess_company_and_title_from_real_subjects():
    g = cr.guess_company
    assert g({"subject": "Update on your application for the AI Engineer at Bluehaven!", "from": "Bluehaven Talent <jobs@bluehaven.com>"}) == "Bluehaven"
    assert g({"subject": "Applicant & Folding Forks—Application received", "from": "no-reply@foldingforks.com"}) == "Folding Forks"
    assert g({"subject": "Seguimiento oferta empleo", "from": "RLM Group Talent Acquisition <talent@rlmgroup.com>"}) == "RLM Group"
    assert g({"subject": "Seguimiento", "from": "noreply@example-corp.com"}) == "Example-corp"
    assert cr.guess_title({"subject": "Your virtual interview at Cordera for R00123456 AI Software Engineer | Spain"}) == "AI Software Engineer"
    assert cr.guess_title({"subject": "Thank you for applying to Cleverfox"}) == ""


def test_the_newest_mail_fixes_the_interview_slot_and_older_times_make_no_event(fake):
    recorded, events = fake
    rep = cr.review(owner="admin", days=14, apply=True, kinds=["interview"])
    acc_events = [e for e in events if "Cordera" in e["summary"]]
    assert len(acc_events) == 1 and acc_events[0]["dtstart"] == "2026-09-22T10:00:00+02:00", events
    invite = next(r for r in rep["found"] if r["uid"] == "9")
    assert invite["superseded_by"] == "2026-09-22T10:00:00+02:00" and invite["interview_at"] is None
    assert invite["action"] == "recorded"          # still a reply on the application
    by_ext = {p["externalId"]: p for _, p in recorded}
    assert "interviewAt" not in by_ext["<m9@acc>"] and by_ext["<m2@acc>"]["interviewAt"] == "2026-09-22T08:00:00Z"
