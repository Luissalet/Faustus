"""CONN-02, over real HTTP: /api/email/send/prepare, /execute/{id}, /reconcile/{id}.

Per COMUN.md rule 7, this drives the actual route handlers through a
`TestClient` against a real (if minimally mounted) FastAPI app — the same
pattern `tests/test_contracts_mcp_and_routes.py` uses: only the router under
test is included, and the owner dependency is overridden the standard
FastAPI way (`app.dependency_overrides`), not monkeypatched string-by-string.
The SMTP socket and IMAP connection are stubbed (there is no mail server in
CI) — this is a transport stub at the one external boundary this process
cannot reach in a test, not a mock of the HTTP behavior under test.
"""
from __future__ import annotations

import os
import smtplib
import tempfile

import pytest

_tmp_data = tempfile.mkdtemp(prefix="odysseus-conn02-http-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data}/app.db")
os.environ["ODYSSEUS_INPROCESS_POLLERS"] = "0"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as db_mod
import routes.email_routes as email_routes
import src.connector_outbox as connector_outbox
from src import approval_store


@pytest.fixture(autouse=True)
def _shared_test_db(monkeypatch):
    """`core.database`'s default `sqlite:///:memory:` engine uses a
    `SingletonThreadPool` — one connection per THREAD. `TestClient` runs the
    app in a different thread than the test itself, so that thread gets a
    brand new, empty `:memory:` database and every route 500s with
    "no such table". `StaticPool` shares the one connection across threads —
    the same fix `tests/test_workflow_store_races.py` uses for its own
    cross-thread scenario, applied here for the cross-thread TestClient call.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    yield


class _FakeImap:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def select(self, name, readonly=True):
        return "OK", [b"1"]

    def append(self, folder, flags, when, msg_bytes):
        return "OK", [b"APPENDUID 1 99"]

    def uid(self, command, *args):
        return "OK", [b""]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(email_routes, "_imap", lambda *a, **k: _FakeImap())
    monkeypatch.setattr(email_routes, "_detect_sent_folder", lambda conn: "Sent")
    monkeypatch.setattr(email_routes, "_resolve_send_config", lambda account_id, owner="": {
        "account_id": account_id or "acct-1", "from_address": "me@example.com",
        "display_name": "Me", "smtp_host": "smtp.example.com", "smtp_port": 465,
        "smtp_user": "me", "smtp_password": "secret",
    })
    monkeypatch.setattr(email_routes, "_assert_owns_account", lambda account_id, owner: None)
    app = FastAPI()
    app.include_router(email_routes.setup_email_routes())
    app.dependency_overrides[email_routes.require_owner] = lambda: "u1"
    return TestClient(app)


def _prepare_body(**over):
    body = {"to": "ana@example.com", "subject": "hi", "body": "hello there"}
    body.update(over)
    return body


def test_prepare_opens_an_approval_and_sends_nothing(client, monkeypatch):
    sent = []
    monkeypatch.setattr(email_routes, "_send_smtp_message",
                        lambda *a, **k: sent.append(a) or None)
    resp = client.post("/api/email/send/prepare", json=_prepare_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "prepared"
    assert sent == [], "prepare must not send anything"


def test_execute_without_approval_is_refused_and_nothing_is_sent(client, monkeypatch):
    sent = []
    monkeypatch.setattr(email_routes, "_send_smtp_message",
                        lambda *a, **k: sent.append(a) or None)
    prep = client.post("/api/email/send/prepare", json=_prepare_body()).json()
    exe = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    assert exe.status_code == 200
    assert exe.json()["ok"] is False
    assert sent == []


def test_prepare_then_approve_then_execute_sends_and_records_external_ref(client, monkeypatch):
    sent = []

    def fake_send(cfg, from_addr, recipients, message, timeout=30):
        sent.append((from_addr, recipients))

    monkeypatch.setattr(email_routes, "_send_smtp_message", fake_send)
    prep = client.post("/api/email/send/prepare", json=_prepare_body()).json()
    decided = approval_store.decide(prep["approval_id"], granted=True, by="u1")
    assert decided["ok"], decided

    exe = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    assert exe.status_code == 200
    result = exe.json()
    assert result["ok"] is True and result["status"] == "executed"
    assert result["external_ref"]
    assert len(sent) == 1 and sent[0][1] == ["ana@example.com"]

    status = client.get(f"/api/email/send/outbox/{prep['outbox_id']}").json()
    assert status["status"] == "executed"

    # A second execute of the same outbox_id must never send again.
    exe2 = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    assert exe2.json()["ok"] is False
    assert len(sent) == 1


def test_smtp_disconnect_after_accept_is_uncertain_and_reconcile_settles_it(client, monkeypatch):
    """QA-11 for the email connector: `smtplib.SMTPServerDisconnected` cannot
    rule out the DATA command having already been accepted."""
    def fake_send(cfg, from_addr, recipients, message, timeout=30):
        raise smtplib.SMTPServerDisconnected("connection lost")

    monkeypatch.setattr(email_routes, "_send_smtp_message", fake_send)
    prep = client.post("/api/email/send/prepare", json=_prepare_body()).json()
    approval_store.decide(prep["approval_id"], granted=True, by="u1")

    exe = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    result = exe.json()
    assert result["ok"] is False
    assert result["reason"] == "outcome_unknown"

    # Blind retry refused — even with a working SMTP now.
    monkeypatch.setattr(email_routes, "_send_smtp_message", lambda *a, **k: None)
    retry = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    assert retry.json()["ok"] is False
    assert retry.json()["reason"] == "needs_reconciliation"

    # Mailbox unreachable: still uncertain, no verdict.
    monkeypatch.setattr(email_routes, "_imap", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    unresolved = client.post(f"/api/email/send/reconcile/{prep['outbox_id']}")
    assert unresolved.json()["reason"] == "still_uncertain"

    # Mailbox reachable and the Message-ID IS in Sent: adopted, no resend.
    class _FoundImap(_FakeImap):
        def uid(self, command, *args):
            return "OK", [b"55"]

    monkeypatch.setattr(email_routes, "_imap", lambda *a, **k: _FoundImap())
    adopted = client.post(f"/api/email/send/reconcile/{prep['outbox_id']}")
    body = adopted.json()
    assert body["ok"] is True and body["reason"] == "adopted"
    status = client.get(f"/api/email/send/outbox/{prep['outbox_id']}").json()
    assert status["status"] == "executed"


def test_a_definite_smtp_refusal_is_failed_not_uncertain(client, monkeypatch):
    def fake_send(cfg, from_addr, recipients, message, timeout=30):
        raise smtplib.SMTPRecipientsRefused({"ana@example.com": (550, b"no such user")})

    monkeypatch.setattr(email_routes, "_send_smtp_message", fake_send)
    prep = client.post("/api/email/send/prepare", json=_prepare_body()).json()
    approval_store.decide(prep["approval_id"], granted=True, by="u1")
    exe = client.post(f"/api/email/send/execute/{prep['outbox_id']}")
    result = exe.json()
    assert result["ok"] is False
    assert result["reason"] == "failed"
    status = client.get(f"/api/email/send/outbox/{prep['outbox_id']}").json()
    assert status["status"] == "failed"


def test_cancel_route(client, monkeypatch):
    monkeypatch.setattr(email_routes, "_send_smtp_message", lambda *a, **k: None)
    prep = client.post("/api/email/send/prepare", json=_prepare_body()).json()
    cancelled = client.delete(f"/api/email/send/outbox/{prep['outbox_id']}")
    assert cancelled.json()["status"] == "cancelled"
