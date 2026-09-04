"""
tests/test_changesets_routes.py — "prove it" over HTTP.

The thing worth pinning here is that a REFUSAL is an answer rather than an
error. "An explore that wrote to four files" is exactly the question somebody
brings to this endpoint, and answering it with a 500 would send them looking
for a bug in Faustus instead of at their own report.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.changesets_routes import setup_changesets_routes


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_changesets_routes())
    return TestClient(app)


GOOD = {
    "intent": "implement",
    "workspace": "D:/proj",
    "checkpoint": "a" * 40,
    "changes": {"source": "checkpoint", "checkpoint": "a" * 40,
                "modified": ["src/limiter.py"]},
    "verification": {"mode": "tests", "ran": True, "ok": True,
                     "command": "pytest -q", "summary": "12 passed"},
    "claims": [{"path": "src/limiter.py", "kind": "modified"}],
}


def test_a_report_backed_by_its_evidence_comes_back_judged(client):
    body = client.post("/api/changesets/build", json=GOOD).json()
    assert body["ok"] is True
    assert body["proof"]["verdict"] in ("proved", "partial", "unproved", "contradicted")
    assert body["unsupported_claims"] == []
    assert len(body["fingerprint"]) == 64
    assert "tests: passed" in body["rendered"]


def test_a_claim_the_evidence_does_not_support_is_returned_not_hidden(client):
    body = client.post("/api/changesets/build", json={
        **GOOD, "claims": [{"path": "src/limiter.py"},
                           {"path": "src/cache.py"}]}).json()
    assert body["ok"] is True
    assert [p["path"] for p in body["unsupported_claims"]] == ["src/cache.py"]
    assert "CLAIMED BUT NOT SEEN: src/cache.py" in body["rendered"]
    assert any(g["kind"] == "claim_not_on_disk" for g in body["gaps"])


def test_a_refusal_is_an_answer_and_not_an_http_error(client):
    """An `explore` that wrote to files is the question, not a bug."""
    out = client.post("/api/changesets/build", json={**GOOD, "intent": "explore"})
    assert out.status_code == 200
    body = out.json()
    assert body["ok"] is False
    assert body["field"] == "changeset.files"
    assert "will be left alone" in body["reason"]


def test_a_result_from_a_run_that_did_not_happen_is_refused_by_field(client):
    body = client.post("/api/changesets/build", json={
        **GOOD, "verification": {"mode": "tests", "ran": False, "ok": True}}).json()
    assert body["ok"] is False
    assert body["field"] == "changeset.verification.ok"


def test_nothing_verified_lowers_the_verdict_and_says_why(client):
    body = client.post("/api/changesets/build", json={
        **GOOD, "verification": {"mode": "none", "ran": False}}).json()
    assert body["proof"]["verdict"] != "proved"
    assert body["proof"]["uncertainty"]
    assert any(g["kind"] == "no_verification_runner" for g in body["gaps"])
    assert "nothing was run to check this" in body["rendered"]


def test_the_same_report_twice_has_the_same_fingerprint(client):
    first = client.post("/api/changesets/build", json=GOOD).json()
    second = client.post("/api/changesets/build", json=GOOD).json()
    assert first["fingerprint"] == second["fingerprint"]
    assert first["changeset"]["id"] != second["changeset"]["id"], (
        "the id is per-report; the fingerprint is per-work")


def test_a_job_that_does_not_exist_is_a_404(client):
    assert client.get("/api/changesets/from-dispatch/nope").status_code == 404


def test_the_diff_endpoint_says_when_there_is_nothing_to_diff_against(client):
    body = client.post("/api/changesets/diff", json={
        "intent": "implement", "workspace": "D:/proj",
        "changes": {"source": "none"}}).json()
    assert body["ok"] is False and body["reason"] == "no_checkpoint"


def test_a_body_that_is_not_json_is_the_only_4xx(client):
    assert client.post("/api/changesets/build", content=b"not json").status_code == 400
