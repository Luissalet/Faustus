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


@pytest.mark.parametrize('method', ['PUT', 'PATCH'])
@pytest.mark.parametrize('text', ['á漢🙂\n' * 20_000 + 'END', 'x' * 3_192_506 + 'END'],
                         ids=['unicode_over_80K_bytes', 'three_million_chars'])
def test_long_draft_put_patch_get_preserves_text_and_omitted_attachments(harness, method, text):
    import hashlib
    sid = _add_session(harness, name="Chat")
    path = f'/api/sessions/{sid}/draft'
    previous = harness.client.put(path, json={'text': 'previous', 'attachment_ids': ['att1']})
    assert previous.status_code == 200
    r = harness.client.request(method, path, json={'text': text})
    assert r.status_code == 200, r.text
    result = harness.client.get(path).json()
    assert len(result['text']) == len(text)
    assert hashlib.sha256(result['text'].encode()).digest() == hashlib.sha256(text.encode()).digest()
    assert result['attachment_ids'] == (['att1'] if method == 'PATCH' else [])
    assert result['updated_at'] >= previous.json()['updated_at'] > 0


def test_patch_attachment_only_preserves_long_text_and_clear_keeps_put_semantics(harness):
    sid = _add_session(harness, name='Chat')
    path = f'/api/sessions/{sid}/draft'
    text = 'á漢🙂' * 30_000
    assert harness.client.put(path, json={'text': text, 'attachment_ids': ['one']}).status_code == 200
    result = harness.client.patch(path, json={'attachment_ids': ['two']})
    assert result.status_code == 200 and result.json()['text'] == text
    assert result.json()['attachment_ids'] == ['two']
    cleared = harness.client.put(path, json={'text': ' \n\t ', 'attachment_ids': []})
    assert cleared.status_code == 200 and cleared.json() == {'text': '', 'attachment_ids': [], 'updated_at': 0}
    assert harness.client.get(path).json() == cleared.json()


@pytest.mark.parametrize('method', ['GET', 'PUT', 'PATCH'])
def test_foreign_owner_long_draft_request_is_still_refused(harness, method):
    sid = _add_session(harness, name='Foreign', owner='bob')
    kwargs = {} if method == 'GET' else {'json': {'text': 'x' * 90_000}}
    assert harness.client.request(method, f'/api/sessions/{sid}/draft', **kwargs).status_code == 404


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
