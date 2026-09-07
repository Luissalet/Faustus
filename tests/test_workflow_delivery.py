"""Production handler, real workflow/approval persistence, inert SMTP only."""
from email import policy
from email.parser import BytesParser

import pytest

from src import approval_store
from src.workflows import WorkflowEngine
from src.workflows.runtime import production_handlers
from tests.test_workflow_handlers import store, wf  # noqa: F401


@pytest.fixture
def transport(monkeypatch):
    import routes.email_routes as accounts
    import routes.email_helpers as smtp
    calls = []
    cfg = {"account_id": "alice-mail", "from_address": "alice@example.com",
           "smtp_host": "smtp.example.com", "smtp_port": 465,
           "smtp_security": "ssl", "smtp_user": "alice@example.com"}
    resolved = []

    def account(account_id, owner):
        resolved.append((account_id, owner))
        return dict(cfg)

    monkeypatch.setattr(accounts, "_resolve_send_config", account)
    monkeypatch.setattr(smtp, "_send_smtp_message", lambda *a, **kw: calls.append((a, kw)))
    return {"calls": calls, "cfg": cfg, "resolved": resolved}


def start(store, config=None, **node_options):
    definition = wf({"id": "send", "type": "deliver", **node_options,
                     "config": config or {"to": "bob@example.com", "subject": "Informe",
                                          "body": "Contenido en español y English."}})
    run_id = store.create_run(definition, owner="alice")["run_id"]
    engine = WorkflowEngine(production_handlers(), store)
    return run_id, engine, engine.advance(run_id)


def test_production_delivery_pauses_then_sends_exact_message_once(store, transport):
    run, engine, first = start(store)
    assert first["status"] == "paused"
    assert transport["calls"] == []
    card = approval_store.get(first["approval_id"])
    assert card.owner == "alice"
    assert "Contenido en español" in card.plan.detail
    assert not store.node_runs(run)["send"].result["delivered"]
    approval_store.decide(card.id, granted=True, by="alice")
    assert engine.resume(run, "send")["status"] == "completed"
    assert engine.advance(run)["status"] == "completed"
    assert len(transport["calls"]) == 1
    args, kwargs = transport["calls"][0]
    message = BytesParser(policy=policy.default).parsebytes(args[3])
    assert str(message["To"]) == "bob@example.com"
    assert str(message["Subject"]) == "Informe"
    assert "Contenido en español y English." in message.get_content()
    assert kwargs["timeout"] == 30
    assert all(owner == "alice" for _, owner in transport["resolved"])
    assert approval_store.get(card.id).status == "consumed"
    assert store.effect_state(run, "send", 1) == "confirmed"


def test_changed_account_cannot_use_an_old_approval(store, transport):
    run, engine, first = start(store)
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    transport["cfg"]["from_address"] = "other@example.com"
    assert engine.resume(run, "send")["status"] == "failed"
    assert not transport["calls"]
    assert approval_store.get(first["approval_id"]).uses_left == 1


def test_unknown_smtp_result_is_recorded_and_not_retried(store, transport, monkeypatch):
    import routes.email_helpers as smtp
    run, engine, first = start(store, {"to": "bob@example.com", "body": "hello",
                                      "idempotent": True}, max_attempts=3)
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise ConnectionError("do not show password=private-value")

    monkeypatch.setattr(smtp, "_send_smtp_message", broken)
    outcome = engine.resume(run, "send")
    assert outcome["status"] == "failed"
    assert calls == [1]
    assert len(store.needs_reconciliation(run_id=run)) == 1
    assert "private-value" not in str(outcome)
    assert "private-value" not in store.node_runs(run)["send"].reason
    assert store.node_runs(run)["send"].result["message_id"].endswith("@faustus.local>")


@pytest.mark.parametrize("config", [
    {"to": "bob@example.com\r\nBcc: x@example.com", "body": "x"},
    {"to": "not-an-address", "body": "x"},
    {"to": "bob@example.com", "body": "x", "subject": "hi\nInjected"},
    {"to": "bob@example.com", "body": "x" * (1024 * 1024 + 1)},
    {"to": "bob@example.com", "body_from": "results.missing.text"},
    {"to": "bob@example.com", "body": "x", "attachments": ["C:/private.txt"]},
])
def test_invalid_delivery_never_reaches_smtp_or_opens_card(store, transport, config):
    _, _, first = start(store, config)
    assert first["status"] == "failed"
    assert not transport["calls"]
    assert approval_store.pending(owner="alice") == []


def test_cancelled_workflow_does_not_send_after_approval(store, transport):
    run, engine, first = start(store)
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    store.set_run_status(run, "cancelled")
    assert engine.resume(run, "send")["status"] == "cancelled"
    assert not transport["calls"]


def test_answered_delivery_continues_without_reopening_the_browser(store, transport):
    from src.workflows.scheduler import WorkflowScheduler
    run, _, first = start(store)
    assert WorkflowScheduler(store).tick() == []
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    assert WorkflowScheduler(store).tick()[0]["status"] == "completed"
    assert len(transport["calls"]) == 1
    assert WorkflowScheduler(store).tick() == []


def test_partial_delivery_keeps_safe_recipient_receipt(store, transport, monkeypatch):
    import smtplib
    import routes.email_helpers as smtp
    run, engine, first = start(store, {"to": ["bob@example.com", "carol@example.com"],
                                      "body": "hello"})
    approval_store.decide(first["approval_id"], granted=True, by="alice")

    def partial(*a, **kw):
        raise smtplib.SMTPRecipientsRefused({"carol@example.com": (550, b"private server response")})

    monkeypatch.setattr(smtp, "_send_smtp_message", partial)
    assert engine.resume(run, "send")["status"] == "failed"
    result = store.node_runs(run)["send"].result
    assert result["accepted_recipients"] == ["bob@example.com"]
    assert result["rejected_recipients"] == ["carol@example.com"]
    assert "private server response" not in str(result)
