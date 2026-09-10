"""CONN-02 — connector_outbox.py: read/prepare/execute as three distinct
steps, reusing approval_store, and QA-11's outcome_unknown for connectors.

No HTTP surface here on purpose: this file exercises the shared mechanism
`src/connector_outbox.py` (used by the new email prepare/execute/reconcile
routes — see tests/test_conn_02_email_send_routes.py for the HTTP-crossing
behavior against the real app, per COMUN.md rule 7).
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp_data = tempfile.mkdtemp(prefix="odysseus-conn02-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data}/app.db")

import src.connector_outbox as connector_outbox
from src import approval_store


def _prepare(owner="u1", payload=None, recipients=("ana@example.com",), idempotency_key=""):
    payload = payload or {"to": "ana@example.com", "subject": "hi", "body": "hello"}
    return connector_outbox.prepare(
        connector="email", action="send", owner=owner, payload=payload,
        recipients=list(recipients), preview="To: ana@example.com",
        idempotency_key=idempotency_key,
    )


def _grant(approval_id, owner="u1"):
    decided = approval_store.decide(approval_id, granted=True, by=owner)
    assert decided["ok"], decided
    return decided


def test_prepare_opens_an_approval_and_has_no_effect():
    result = _prepare()
    assert result["ok"] is True
    assert result["status"] == "prepared"
    approval = approval_store.get(result["approval_id"])
    assert approval is not None and approval.status == "pending"
    record = connector_outbox.get(result["outbox_id"], owner="u1")
    assert record["status"] == "prepared"
    assert record["external_ref"] == ""


def test_execute_refuses_without_a_granted_approval():
    result = _prepare()
    outcome = connector_outbox.execute(result["outbox_id"], owner="u1",
                                       run=lambda p: {"ok": True, "external_ref": "x"})
    assert outcome["ok"] is False
    # not granted -> approval_store.consume refuses (never a blind run;
    # nothing external was called)
    assert outcome["reason"] in ("no_approval", "plan_changed", "status_pending")


def test_execute_runs_exactly_once_and_the_second_call_is_refused():
    result = _prepare()
    _grant(result["approval_id"])
    calls = []

    def run(payload):
        calls.append(payload)
        return {"ok": True, "external_ref": "msg-1"}

    first = connector_outbox.execute(result["outbox_id"], owner="u1", run=run)
    assert first["ok"] is True and first["status"] == "executed"
    assert len(calls) == 1

    second = connector_outbox.execute(result["outbox_id"], owner="u1", run=run)
    assert second["ok"] is False
    assert second["reason"] == "already_executed"
    assert len(calls) == 1, "a second execute() must never call run() again"


def test_changing_the_payload_after_prepare_invalidates_the_approval():
    """CONN-02: 'cambiar el payload invalida la aprobación'. The outbox API
    itself never lets a caller mutate a prepared row's payload — this proves
    the underlying mechanism (payload_digest folded into the approval plan's
    fingerprinted `detail`) actually enforces it, by simulating the one way
    a payload could still drift: the row is edited directly (e.g. a future
    connector that allows amending a draft before send)."""
    result = _prepare()
    _grant(result["approval_id"])

    # Simulate a payload edited after the approval was granted.
    conn = connector_outbox._connect()
    try:
        conn.execute(
            "UPDATE connector_outbox SET payload_json = ?, payload_digest = ? WHERE id = ?",
            ('{"to": "eve@example.com", "subject": "hi", "body": "hello"}',
             "deadbeef" * 8, result["outbox_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    outcome = connector_outbox.execute(result["outbox_id"], owner="u1",
                                       run=lambda p: {"ok": True, "external_ref": "x"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "plan_changed"


def test_run_raising_uncertain_leaves_the_row_uncertain_never_failed_never_retried():
    """QA-11 for connectors: the service may have accepted the effect but the
    confirmation was lost. Must never look like a definite failure and must
    never be blindly retried."""
    result = _prepare()
    _grant(result["approval_id"])

    def run(payload):
        raise connector_outbox.ConnectorEffectUncertain("connection reset after DATA")

    outcome = connector_outbox.execute(result["outbox_id"], owner="u1", run=run)
    assert outcome["ok"] is False
    assert outcome["reason"] == "outcome_unknown"
    assert connector_outbox.get(result["outbox_id"], owner="u1")["status"] == "uncertain"

    blind_retry = connector_outbox.execute(
        result["outbox_id"], owner="u1", run=lambda p: {"ok": True, "external_ref": "x"})
    assert blind_retry["ok"] is False
    assert blind_retry["reason"] == "needs_reconciliation"


def test_a_refused_run_is_a_definite_failure_not_uncertain():
    result = _prepare()
    _grant(result["approval_id"])

    def run(payload):
        raise RuntimeError("recipient rejected: 550 no such user")

    outcome = connector_outbox.execute(result["outbox_id"], owner="u1", run=run)
    assert outcome["ok"] is False
    assert outcome["reason"] == "failed"
    assert connector_outbox.get(result["outbox_id"], owner="u1")["status"] == "failed"


def test_reconcile_adopts_when_the_destination_confirms_and_never_resends():
    result = _prepare()
    _grant(result["approval_id"])
    connector_outbox.execute(
        result["outbox_id"], owner="u1",
        run=lambda p: (_ for _ in ()).throw(connector_outbox.ConnectorEffectUncertain("lost")))

    # Destination unreachable right now: stays uncertain, no verdict yet.
    still = connector_outbox.reconcile(result["outbox_id"], owner="u1", find=lambda p: None)
    assert still["reason"] == "still_uncertain"
    assert connector_outbox.get(result["outbox_id"], owner="u1")["status"] == "uncertain"

    # Destination reachable and confirms it: adopted, never a second send.
    adopted = connector_outbox.reconcile(
        result["outbox_id"], owner="u1",
        find=lambda p: {"found": True, "external_ref": "adopted-id"})
    assert adopted["ok"] is True and adopted["reason"] == "adopted"
    record = connector_outbox.get(result["outbox_id"], owner="u1")
    assert record["status"] == "executed"
    assert record["external_ref"] == "adopted-id"


def test_reconcile_fails_the_row_when_the_destination_genuinely_has_nothing():
    result = _prepare()
    _grant(result["approval_id"])
    connector_outbox.execute(
        result["outbox_id"], owner="u1",
        run=lambda p: (_ for _ in ()).throw(connector_outbox.ConnectorEffectUncertain("lost")))

    settled = connector_outbox.reconcile(result["outbox_id"], owner="u1",
                                         find=lambda p: {"found": False})
    assert settled["reason"] == "never_landed"
    assert connector_outbox.get(result["outbox_id"], owner="u1")["status"] == "failed"


def test_owner_scoping_hides_another_owners_row():
    result = _prepare(owner="alice")
    assert connector_outbox.get(result["outbox_id"], owner="bob") is None
    outcome = connector_outbox.execute(result["outbox_id"], owner="bob",
                                       run=lambda p: {"ok": True, "external_ref": "x"})
    assert outcome["ok"] is False and outcome["reason"] == "not_found"


def test_cancel_only_works_on_a_prepared_row_and_is_idempotent():
    result = _prepare()
    cancelled = connector_outbox.cancel(result["outbox_id"], owner="u1")
    assert cancelled["ok"] is True and cancelled["status"] == "cancelled"
    # A retried cancel (or a cleanup pass) on an already-cancelled row is not
    # a refusal: the caller asked for a state and the row is in it, the same
    # idempotent shape `src/media_runs.py::cancel` uses for "already_cancelled".
    again = connector_outbox.cancel(result["outbox_id"], owner="u1")
    assert again["ok"] is True and again["reason"] == "already_cancelled"

    granted_result = _prepare()
    _grant(granted_result["approval_id"])
    connector_outbox.execute(granted_result["outbox_id"], owner="u1",
                             run=lambda p: {"ok": True, "external_ref": "x"})
    refused = connector_outbox.cancel(granted_result["outbox_id"], owner="u1")
    assert refused["ok"] is False and refused["reason"] == "already_executed"


def test_needs_reconciliation_lists_only_uncertain_rows_for_the_owner():
    result = _prepare(owner="alice")
    _grant(result["approval_id"], owner="alice")
    connector_outbox.execute(
        result["outbox_id"], owner="alice",
        run=lambda p: (_ for _ in ()).throw(connector_outbox.ConnectorEffectUncertain("x")))
    queue = connector_outbox.needs_reconciliation(owner="alice")
    assert any(r["id"] == result["outbox_id"] for r in queue)
    assert connector_outbox.needs_reconciliation(owner="bob") == []
