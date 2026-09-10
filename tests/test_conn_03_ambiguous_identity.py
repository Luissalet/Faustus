"""CONN-03 — resolving a name against contacts must not silently pick a
winner when more than one candidate ties for the best match; it must
either ask the user or fail with `ambiguous_identity`, listing candidates.

Two surfaces implement this independently (deliberately not sharing an
import across `routes/email_routes.py` and `routes/contacts/contacts_routes.py`
— see CLAUDE.md's "PROPIOS" split): the Sent-folder-derived contact
resolver in email_routes, and the local/CardDAV contact search in
contacts_routes. Both are exercised here, over real HTTP per COMUN.md
rule 7, plus direct unit tests of the shared scoring rule each keeps
its own copy of.
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp_data = tempfile.mkdtemp(prefix="odysseus-conn03-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data}/app.db")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.email_routes as email_routes
import routes.contacts.contacts_routes as contacts_routes


# ---------------------------------------------------------------------------
# Unit tests: the scoring rule itself (each module keeps its own copy).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", [email_routes, contacts_routes])
def test_identity_match_score_exact_beats_substring(mod):
    score = mod._identity_match_score
    assert score("Ana", "Ana") == 1.0
    assert score("ana", "Ana") == 1.0, "case-insensitive"
    assert score("Ana", "Ana Garcia") == 0.75, "whole-word match in a full name"
    assert score("Ana", "Anabel Ruiz") == 0.5, "substring-only match scores lower"
    assert score("Ana", "Bob") == 0.0
    assert score("", "Ana") == 0.0
    assert score("Ana", "") == 0.0


# ---------------------------------------------------------------------------
# HTTP: routes/email_routes.py — GET /api/email/resolve-contact
# ---------------------------------------------------------------------------

class _FakeImapTwoAnas:
    """Sent folder containing two distinct people both named plain 'Ana'."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def select(self, name, readonly=True):
        if name != '"Sent"':
            return "NO", [b""]
        return "OK", [b"2"]

    def search(self, charset, criteria):
        return "OK", [b"1 2"]

    def fetch(self, uid, spec):
        n = int(uid)
        if n == 1:
            raw = b'From: "Ana Lopez" <ana.lopez@example.com>\r\nTo: me@example.com\r\n'
        else:
            raw = b'From: "Ana Ruiz" <ana.ruiz@example.com>\r\nTo: me@example.com\r\n'
        return "OK", [(b"1", raw)]


class _FakeImapOneAna(_FakeImapTwoAnas):
    """Same 'Ana' appearing twice (e.g. From once, To once) — not ambiguous:
    a single identity, not two candidates."""

    def fetch(self, uid, spec):
        raw = b'From: "Ana Lopez" <ana.lopez@example.com>\r\nTo: "Ana Lopez" <ana.lopez@example.com>\r\n'
        return "OK", [(b"1", raw)]


@pytest.fixture()
def email_client():
    app = FastAPI()
    app.include_router(email_routes.setup_email_routes())
    app.dependency_overrides[email_routes.require_owner] = lambda: "u1"
    return TestClient(app)


def test_resolve_contact_two_distinct_anas_is_ambiguous(email_client, monkeypatch):
    monkeypatch.setattr(email_routes, "_imap", lambda *a, **k: _FakeImapTwoAnas())
    resp = email_client.get("/api/email/resolve-contact", params={"name": "Ana"})
    assert resp.status_code == 200
    body = resp.json()
    # Backward compat: existing keys unchanged in shape.
    assert isinstance(body["contacts"], list) and body["query"] == "Ana"
    assert body["ambiguous"] is True
    assert body["reason"] == "ambiguous_identity"
    emails = {c["email"] for c in body["candidates"]}
    assert emails == {"ana.lopez@example.com", "ana.ruiz@example.com"}


def test_resolve_contact_single_identity_is_not_ambiguous(email_client, monkeypatch):
    monkeypatch.setattr(email_routes, "_imap", lambda *a, **k: _FakeImapOneAna())
    resp = email_client.get("/api/email/resolve-contact", params={"name": "Ana"})
    body = resp.json()
    assert body["ambiguous"] is False
    assert "reason" not in body and "candidates" not in body
    assert body["contacts"] == [{"email": "ana.lopez@example.com", "name": "Ana Lopez"}]


# ---------------------------------------------------------------------------
# HTTP: routes/contacts/contacts_routes.py — GET /api/contacts/search
# ---------------------------------------------------------------------------

@pytest.fixture()
def contacts_client():
    app = FastAPI()
    app.include_router(contacts_routes.setup_contacts_routes())
    app.dependency_overrides[contacts_routes.require_admin] = lambda: "admin"
    return TestClient(app)


_TWO_ANAS = [
    {"uid": "1", "name": "Ana Lopez", "emails": ["ana.lopez@example.com"], "phones": []},
    {"uid": "2", "name": "Ana Ruiz", "emails": ["ana.ruiz@example.com"], "phones": []},
]

_ONE_ANA = [
    {"uid": "1", "name": "Ana Lopez", "emails": ["ana.lopez@example.com"], "phones": []},
    {"uid": "3", "name": "Bob Smith", "emails": ["bob@example.com"], "phones": []},
]


def test_contacts_search_two_distinct_anas_is_ambiguous(contacts_client, monkeypatch):
    monkeypatch.setattr(contacts_routes, "_fetch_contacts", lambda force=False: _TWO_ANAS)
    resp = contacts_client.get("/api/contacts/search", params={"q": "Ana"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2, "backward compat: results still lists all matches"
    assert body["ambiguous"] is True
    assert body["reason"] == "ambiguous_identity"
    names = {c["name"] for c in body["candidates"]}
    assert names == {"Ana Lopez", "Ana Ruiz"}


def test_contacts_search_one_match_is_not_ambiguous(contacts_client, monkeypatch):
    monkeypatch.setattr(contacts_routes, "_fetch_contacts", lambda force=False: _ONE_ANA)
    resp = contacts_client.get("/api/contacts/search", params={"q": "Ana"})
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["ambiguous"] is False
    assert "candidates" not in body


def test_contacts_search_empty_query_is_unchanged(contacts_client, monkeypatch):
    monkeypatch.setattr(contacts_routes, "_fetch_contacts", lambda force=False: _TWO_ANAS)
    resp = contacts_client.get("/api/contacts/search", params={"q": ""})
    assert resp.json() == {"results": []}
