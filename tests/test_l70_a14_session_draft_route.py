"""Lote 70a, punto A.14 (route half) — GET/PUT /api/sessions/{sid}/draft.

Reuses `tests/test_session_export_routes.py`'s own harness idiom
(`harness`/`_add_session`, real temp DB, real router) rather than
duplicating it — this route lives in the same `routes/session_routes.py`
factory.
"""
from __future__ import annotations

import uuid

import pytest

from tests.test_session_export_routes import (
    _add_session,
    harness,  # noqa: F401 - reused fixture
)


@pytest.fixture(autouse=True)
def _isolated_draft_store(tmp_path, monkeypatch):
    """Never let a draft saved by this file land under the process-wide
    real DATA_DIR shared with an actual running Faustus instance."""
    from src import session_draft
    monkeypatch.setattr(session_draft, "DATA_DIR", tmp_path / "drafts")


def test_get_with_no_saved_draft_is_the_empty_record(harness):
    sid = _add_session(harness, name="Chat")
    r = harness.client.get(f"/api/sessions/{sid}/draft")
    assert r.status_code == 200
    assert r.json() == {"text": "", "attachment_ids": [], "updated_at": 0}


def test_put_then_get_round_trips(harness):
    sid = _add_session(harness, name="Chat")
    r = harness.client.put(f"/api/sessions/{sid}/draft",
                           json={"text": "Half a sentence", "attachment_ids": ["att1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Half a sentence"
    assert body["attachment_ids"] == ["att1"]
    assert body["updated_at"] > 0

    r2 = harness.client.get(f"/api/sessions/{sid}/draft")
    assert r2.json() == body


def test_put_empty_text_and_no_attachments_clears_it(harness):
    sid = _add_session(harness, name="Chat")
    harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "keep typing"})
    r = harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "", "attachment_ids": []})
    assert r.status_code == 200
    assert r.json() == {"text": "", "attachment_ids": [], "updated_at": 0}


def test_another_users_session_404s_on_both_verbs(harness):
    sid = _add_session(harness, name="Bob's", owner="bob")
    assert harness.client.get(f"/api/sessions/{sid}/draft").status_code == 404
    assert harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "sneaky"}).status_code == 404


def test_an_unknown_session_404s(harness):
    sid = str(uuid.uuid4())
    assert harness.client.get(f"/api/sessions/{sid}/draft").status_code == 404


def test_put_over_the_body_cap_is_413(harness):
    sid = _add_session(harness, name="Chat")
    r = harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "x" * 90_000})
    assert r.status_code == 413


def test_put_with_a_non_object_body_is_a_client_error(harness):
    sid = _add_session(harness, name="Chat")
    r = harness.client.put(f"/api/sessions/{sid}/draft", json=["not", "an", "object"])
    assert r.status_code == 409


def test_two_sessions_keep_separate_drafts(harness):
    sid_a = _add_session(harness, name="A")
    sid_b = _add_session(harness, name="B")
    harness.client.put(f"/api/sessions/{sid_a}/draft", json={"text": "Draft A"})
    harness.client.put(f"/api/sessions/{sid_b}/draft", json={"text": "Draft B"})
    assert harness.client.get(f"/api/sessions/{sid_a}/draft").json()["text"] == "Draft A"
    assert harness.client.get(f"/api/sessions/{sid_b}/draft").json()["text"] == "Draft B"
