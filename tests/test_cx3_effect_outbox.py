"""H03: write-ahead outbox for effects that cannot be repeated.

The receivers below are real sockets in this process (no network, no real
mail): they commit the action and then behave badly, which is exactly the
situation a caller cannot tell apart from "it never went".
"""
import asyncio
import email.message
import json
import socketserver
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src import effect_outbox as eo


@pytest.fixture
def outbox(tmp_path):
    eo.set_db_path(str(tmp_path / "effects.sqlite3"))
    yield eo
    eo.set_db_path(None)


# ---------------------------------------------------------------------------
# A synthetic SMTP receiver: commits the message, then cuts the connection
# ---------------------------------------------------------------------------

class SmtpReceiver:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.messages = []
        receiver = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda line: self.wfile.write(line.encode() + b"\r\n")
                w("220 synthetic")
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    cmd = raw.decode("latin-1").strip()
                    up = cmd.upper()
                    if up.startswith(("EHLO", "HELO")):
                        w("250 synthetic")
                    elif up.startswith("MAIL"):
                        w("250 ok")
                    elif up.startswith("RCPT"):
                        if receiver.mode == "reject_rcpt":
                            w("550 no such user")
                        else:
                            w("250 ok")
                    elif up == "DATA":
                        w("354 go ahead")
                        body = []
                        while True:
                            line = self.rfile.readline()
                            if not line or line in (b".\r\n", b".\n"):
                                break
                            body.append(line)
                        if receiver.mode == "cut_before_commit":
                            return  # dies before it stored anything
                        receiver.messages.append(b"".join(body).decode("utf-8", "replace"))
                        if receiver.mode == "commit_then_cut":
                            return  # committed, and never answers
                        if receiver.mode == "data_refused":
                            receiver.messages.pop()
                            w("554 message refused")
                            continue
                        w("250 queued")
                    elif up == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def cfg(self):
        return {"smtp_host": "127.0.0.1", "smtp_port": self.port, "smtp_security": "none",
                "smtp_user": "", "smtp_password": "", "owner": "alice"}

    def find(self, record):
        """Reconciler lookup by the Message-ID the effect carried."""
        mid = record["identifier"]
        return {"found": any(mid in m for m in self.messages), "authoritative": True,
                "external_ref": mid}


def _message(subject="hello"):
    msg = email.message.EmailMessage()
    msg["From"] = "me@example.test"
    msg["To"] = "you@example.test"
    msg["Subject"] = subject
    msg.set_content("body")
    return msg.as_string()


@pytest.fixture
def smtp_receiver():
    made = []

    def make(mode="ok"):
        r = SmtpReceiver(mode)
        made.append(r)
        return r
    yield make
    for r in made:
        r.close()


@pytest.fixture
def send(outbox):
    from routes.email_helpers import _send_smtp_message
    return _send_smtp_message


# ---------------------------------------------------------------------------
# The decisive test
# ---------------------------------------------------------------------------

def test_receiver_commits_and_cuts_the_connection_is_unknown_reconciled_and_never_resent(
        outbox, smtp_receiver, send):
    from routes.email_helpers import SmtpOutcomeUnknown
    receiver = smtp_receiver("commit_then_cut")

    with pytest.raises(SmtpOutcomeUnknown) as first:
        send(receiver.cfg(), "me@example.test", ["you@example.test"], _message(), dedup_key="job-1")

    assert len(receiver.messages) == 1  # the receiver really has it
    row = outbox.list_effects()[0]
    assert row["state"] == "outcome_unknown" and row["effect_certainty"] == "unknown"
    assert row["identifier_kind"] == "message_id" and row["identifier"] in receiver.messages[0]
    assert first.value.effect["id"] == row["id"] and first.value.effect_certainty == "unknown"
    assert isinstance(first.value, OSError)  # existing `except smtplib.SMTPException` still matches

    # A second attempt of the same logical message is refused, not re-sent.
    with pytest.raises(SmtpOutcomeUnknown):
        send(receiver.cfg(), "me@example.test", ["you@example.test"], _message(), dedup_key="job-1")
    assert len(receiver.messages) == 1

    # Reconciliation asks the destination by identifier and adopts what it finds.
    result = outbox.reconcile(row["id"], find=receiver.find)
    assert result["reason"] == "landed" and result["effect"]["state"] == "reconciled"
    assert result["effect"]["resolution"] == "landed" and result["effect"]["effect_certainty"] == "confirmed"

    # Once reconciled as landed, the logical message is done: still not re-sent.
    assert send(receiver.cfg(), "me@example.test", ["you@example.test"], _message(), dedup_key="job-1") is None
    assert len(receiver.messages) == 1


def test_not_landed_is_only_concluded_from_an_authoritative_absence(outbox, smtp_receiver, send):
    from routes.email_helpers import SmtpOutcomeUnknown
    receiver = smtp_receiver("cut_before_commit")
    with pytest.raises(SmtpOutcomeUnknown):
        send(receiver.cfg(), "me@example.test", ["you@example.test"], _message(), dedup_key="job-2")
    row = outbox.list_effects()[0]
    assert row["state"] == "outcome_unknown" and receiver.messages == []

    # A source that does not prove absence leaves the row unknown.
    weak = outbox.reconcile(row["id"], find=lambda rec: {"found": False})
    assert weak["reason"] == "not_found_yet" and weak["effect"]["state"] == "outcome_unknown"
    # An unreachable destination leaves it too.
    assert outbox.reconcile(row["id"], find=lambda rec: None)["reason"] == "still_unknown"
    # Authoritative absence: never landed, and only then may the message go again.
    done = outbox.reconcile(row["id"], find=receiver.find)
    assert done["reason"] == "not_landed" and done["effect"]["effect_certainty"] == "none"
    assert [e["check"] for e in done["effect"]["evidence"]] == ["reconcile"] * 3
    receiver.mode = "ok"
    send(receiver.cfg(), "me@example.test", ["you@example.test"], _message(), dedup_key="job-2")
    assert len(receiver.messages) == 1


def test_refusals_before_the_body_are_plain_failures_and_keep_their_exception_type(
        outbox, smtp_receiver, send):
    import smtplib
    refusing = smtp_receiver("reject_rcpt")
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send(refusing.cfg(), "me@example.test", ["you@example.test"], _message())
    data_refused = smtp_receiver("data_refused")
    with pytest.raises(smtplib.SMTPDataError):
        send(data_refused.cfg(), "me@example.test", ["you@example.test"], _message())
    # Nothing is listening at all.
    dead = smtp_receiver("ok")
    cfg = dead.cfg()
    dead.close()
    with pytest.raises(OSError):
        send(cfg, "me@example.test", ["you@example.test"], _message())
    states = sorted((r["state"], r["effect_certainty"]) for r in outbox.list_effects())
    assert states == [("failed_before_effect", "none")] * 3


def test_partial_delivery_is_recorded_as_partial_and_keeps_the_original_exception(
        outbox, smtp_receiver, send, monkeypatch):
    import smtplib
    receiver = smtp_receiver("ok")
    real = smtplib.SMTP.sendmail

    def partial(self, sender, recipients, message, *a, **k):
        real(self, sender, recipients[:1], message, *a, **k)
        return {recipients[1]: (550, b"no such user")}

    monkeypatch.setattr(smtplib.SMTP, "sendmail", partial)
    with pytest.raises(smtplib.SMTPRecipientsRefused) as err:
        send(receiver.cfg(), "me@example.test", ["a@example.test", "b@example.test"], _message())
    assert err.value.effect_certainty == "partial" and len(receiver.messages) == 1
    row = outbox.list_effects()[0]
    assert row["state"] == "partial" and row["effect_certainty"] == "partial"
    assert outbox.needs_reconciliation()[0]["id"] == row["id"]


def test_generated_message_id_is_added_but_a_bare_body_is_sent_verbatim(outbox, smtp_receiver, send):
    receiver = smtp_receiver("ok")
    send(receiver.cfg(), "me@example.test", ["you@example.test"], _message())
    assert "Message-ID: <" in receiver.messages[0]
    send(receiver.cfg(), "me@example.test", ["you@example.test"], "just a body")
    assert receiver.messages[1].strip() == "just a body"


# ---------------------------------------------------------------------------
# Admission: if the intent is not persisted, the effect is not sent
# ---------------------------------------------------------------------------

def test_unwritable_outbox_means_nothing_is_sent(tmp_path, smtp_receiver):
    from routes.email_helpers import _send_smtp_message
    eo.set_db_path(str(tmp_path))  # a directory: sqlite cannot open it
    try:
        receiver = smtp_receiver("ok")
        with pytest.raises(RuntimeError, match="intent could not be persisted"):
            _send_smtp_message(receiver.cfg(), "me@example.test", ["you@example.test"], _message())
        assert receiver.messages == []
        sent = []
        with pytest.raises(eo.IntentNotPersisted):
            eo.dispatch_effect("x.y", lambda: sent.append(1))
        assert sent == []
    finally:
        eo.set_db_path(None)


def test_admission_reads_the_row_back_and_refuses_when_it_is_not_there(outbox, monkeypatch):
    ran = []
    monkeypatch.setattr(eo, "get", lambda *a, **k: None)
    with pytest.raises(eo.IntentNotPersisted):
        eo.dispatch_effect("x.y", lambda: ran.append(1))
    assert ran == []


def test_dispatching_is_persisted_before_the_transport_is_touched(outbox):
    seen = []

    def transport():
        seen.append(eo.list_effects()[0]["state"])
        return {"id": "remote-1"}

    out = eo.dispatch_effect("x.y", transport, destination="d", arguments={"a": 1})
    assert seen == ["dispatching"]
    assert out.effect["state"] == "succeeded" and out.effect["external_ref"] == "remote-1"
    assert [e["to_state"] for e in eo.events(out.effect["id"])] == [
        "prepared", "admitted", "dispatching", "succeeded"]


def test_state_machine_refuses_illegal_moves(outbox):
    row = eo.prepare(kind="x.y", destination="d")
    with pytest.raises(eo.IntentNotPersisted):
        eo.begin_dispatch(row["id"])  # not admitted yet: the transport must not be touched
    eo.admit(row["id"])
    eo.begin_dispatch(row["id"])
    done = eo.settle(row["id"], "succeeded")
    with pytest.raises(eo.InvalidTransition):
        eo.settle(row["id"], "outcome_unknown")  # a settled row never changes its mind
    assert done["effect_certainty"] == "confirmed"
    with pytest.raises(eo.InvalidTransition):
        eo.cancel(row["id"])


def test_cancelled_keeps_its_own_certainty(outbox):
    before = eo.prepare(kind="x.y")
    assert eo.cancel(before["id"])["effect_certainty"] == "none"
    during = eo.prepare(kind="x.y")
    eo.admit(during["id"])
    eo.begin_dispatch(during["id"])
    cancelled = eo.cancel(during["id"], reason="user stopped")
    assert cancelled["state"] == "cancelled" and cancelled["effect_certainty"] == "unknown"
    assert [r["id"] for r in eo.needs_reconciliation()] == [during["id"]]
    # Cancelling while dispatching blocks a blind repeat of the same logical effect.
    a = eo.prepare(kind="x.y", dedup_key="k")
    eo.admit(a["id"])
    eo.begin_dispatch(a["id"])
    eo.cancel(a["id"])
    with pytest.raises(eo.OutcomeUnknownError):
        eo.dispatch_effect("x.y", lambda: 1 / 0, dedup_key="k")


@pytest.mark.asyncio
async def test_async_cancellation_during_dispatch_is_recorded_and_propagates(outbox):
    started = asyncio.Event()

    async def transport():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(eo.adispatch_effect("x.y", transport, destination="d"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = eo.list_effects()[0]
    assert row["state"] == "cancelled" and row["effect_certainty"] == "unknown"


def test_generic_exception_after_dispatch_is_unknown_not_failed(outbox):
    def boom():
        raise TimeoutError("read timed out")

    with pytest.raises(eo.OutcomeUnknownError) as err:
        eo.dispatch_effect("x.y", boom, destination="d")
    assert isinstance(err.value.__cause__, TimeoutError)
    row = eo.list_effects()[0]
    assert row["state"] == "outcome_unknown" and row["error_class"] == "TimeoutError"


def test_explicit_markers_make_a_failure_certain(outbox):
    def nothing_left():
        raise eo.EffectNotDispatched("connection refused")

    with pytest.raises(eo.EffectNotDispatched):
        eo.dispatch_effect("x.y", nothing_left)

    def refused():
        raise eo.EffectRejected("403")

    with pytest.raises(eo.EffectRejected):
        eo.dispatch_effect("x.y", refused)
    assert {r["effect_certainty"] for r in eo.list_effects()} == {"none"}


def test_interpreting_a_returned_value_can_make_it_unknown(outbox):
    out = eo.dispatch_effect("x.y", lambda: 502, interpret=lambda v: (
        "outcome_unknown", {"reason": "HTTP 502 after the request was sent"}))
    assert out.effect["state"] == "outcome_unknown" and "502" in out.effect["reason"]


# ---------------------------------------------------------------------------
# Idempotency is described, never assumed
# ---------------------------------------------------------------------------

def test_idempotency_scope_is_destination_enforced_only_when_declared(outbox):
    plain = eo.prepare(kind="x.y", idempotency_key="abc")
    declared = eo.prepare(kind="x.y", idempotency_key="abc", destination_enforces_idempotency=True)
    assert plain["idempotency_scope"] == "none" and plain["dedup_scope"] == "none"
    assert declared["idempotency_scope"] == "destination_enforced"


def test_local_dedup_has_a_defined_scope_and_only_refuses(outbox):
    calls = []
    first = eo.dispatch_effect("x.y", lambda: calls.append(1) or {"id": "r1"}, owner="alice", dedup_key="k1")
    again = eo.dispatch_effect("x.y", lambda: calls.append(2), owner="alice", dedup_key="k1")
    assert calls == [1] and first.dispatched and not again.dispatched
    # Another owner, another kind: different scope, dispatched.
    eo.dispatch_effect("x.y", lambda: calls.append(3), owner="bob", dedup_key="k1")
    eo.dispatch_effect("x.z", lambda: calls.append(4), owner="alice", dedup_key="k1")
    assert calls == [1, 3, 4]


def test_failed_before_effect_does_not_block_a_retry(outbox):
    def refused():
        raise eo.EffectNotDispatched("no route")

    with pytest.raises(eo.EffectNotDispatched):
        eo.dispatch_effect("x.y", refused, dedup_key="k")
    out = eo.dispatch_effect("x.y", lambda: {"id": "ok"}, dedup_key="k")
    assert out.dispatched and out.effect["state"] == "succeeded"


# ---------------------------------------------------------------------------
# Recovery after a restart
# ---------------------------------------------------------------------------

def test_restart_recovery_turns_dispatching_into_unknown_and_unsent_intents_into_certain_nothing(
        outbox):
    mid = eo.prepare(kind="x.y", identifier="<m1>", identifier_kind="message_id")
    eo.admit(mid["id"])
    eo.begin_dispatch(mid["id"])
    waiting = eo.prepare(kind="x.y")
    admitted = eo.prepare(kind="x.y")
    eo.admit(admitted["id"])
    with sqlite3.connect(eo.db_path()) as conn:  # rows written by an earlier process
        conn.execute("UPDATE effect_outbox SET boot_id = 'previous-process'")
    counts = eo.recover_orphans()
    assert counts == {"outcome_unknown": 1, "cancelled": 2}
    assert eo.get(mid["id"])["state"] == "outcome_unknown"
    assert eo.get(mid["id"])["effect_certainty"] == "unknown"
    for row in (waiting, admitted):
        now = eo.get(row["id"])
        assert now["state"] == "cancelled" and now["effect_certainty"] == "none"
    # Rows of the running process are never touched.
    live = eo.prepare(kind="x.y")
    assert eo.recover_orphans() == {"outcome_unknown": 0, "cancelled": 0}
    assert eo.get(live["id"])["state"] == "prepared"


def test_manual_resolution_is_recorded_with_its_evidence(outbox):
    def boom():
        raise TimeoutError("x")

    with pytest.raises(eo.OutcomeUnknownError):
        eo.dispatch_effect("x.y", boom)
    row = eo.list_effects()[0]
    assert eo.reconcile(row["id"])["reason"] == "no_reconciler"
    done = eo.resolve_manually(row["id"], landed=True, note="saw it in the destination", actor="alice")
    assert done["effect"]["state"] == "reconciled" and done["effect"]["resolution"] == "manual_landed"
    assert done["effect"]["evidence"][-1]["actor"] == "alice"
    assert eo.resolve_manually(row["id"], landed=False)["reason"].startswith("not_reconcilable")


def test_reconcile_pending_uses_the_registered_reconciler(outbox):
    eo.register_reconciler("x.rec", lambda rec: {"found": True, "external_ref": rec["identifier"]})
    try:
        def boom():
            raise TimeoutError("x")
        with pytest.raises(eo.OutcomeUnknownError):
            eo.dispatch_effect("x.rec", boom, identifier="id-1", identifier_kind="client_request_id")
        results = eo.reconcile_pending()
        assert results and results[0]["reason"] == "landed"
        assert eo.summary()["unresolved"] == 0
    finally:
        eo._RECONCILERS.pop("x.rec", None)


def test_disabled_setting_restores_plain_dispatch(outbox, monkeypatch):
    monkeypatch.setattr(eo, "enabled", lambda: False)
    out = eo.dispatch_effect("x.y", lambda: 41 + 1)
    assert out.value == 42 and eo.list_effects() == []


# ---------------------------------------------------------------------------
# A second real transport: an HTTP bridge that commits and then drops the socket
# ---------------------------------------------------------------------------

class HttpReceiver:
    def __init__(self, mode):
        self.mode = mode
        self.received = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if receiver.mode == "refuse":
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "forbidden"}')
                    return
                receiver.received.append(body)
                if receiver.mode == "commit_then_cut":
                    self.connection.close()  # no response at all
                    return
                payload = json.dumps({"id": "wa-1", "to": body.get("to")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bridge(outbox, monkeypatch):
    from src import whatsapp_bridge as wa
    made = []

    def make(mode):
        r = HttpReceiver(mode)
        made.append(r)
        monkeypatch.setattr(wa, "base_url", lambda: f"http://127.0.0.1:{r.port}")
        monkeypatch.setattr(wa, "_headers", lambda: {})
        return r
    yield wa, make
    for r in made:
        r.close()


def test_bridge_send_that_was_committed_but_not_answered_is_unknown_and_reconciled_by_content(bridge):
    wa, make = bridge
    receiver = make("commit_then_cut")
    with pytest.raises(wa.BridgeOutcomeUnknown) as err:
        wa.send("34600000000", "see you at 8")
    assert len(receiver.received) == 1 and err.value.effect_certainty == "unknown"
    assert isinstance(err.value, wa.BridgeError)  # the tools' `except BridgeError` still applies
    row = eo.list_effects()[0]
    assert row["kind"] == "whatsapp.send" and row["state"] == "outcome_unknown"
    assert row["identifier_kind"] == "content_fingerprint"

    # Reconcile by the content fingerprint against what the chat shows.
    sent = {"from_me": True, "id": "wamid-7", "ts": 4102444800,
            "text": "see you at 8"}
    wa_messages = lambda *a, **k: [sent]
    original = wa.messages
    wa.messages = wa_messages
    try:
        result = eo.reconcile(row["id"])
    finally:
        wa.messages = original
    assert result["reason"] == "landed" and result["effect"]["external_ref"] == "wamid-7"
    assert len(receiver.received) == 1  # never sent again


def test_bridge_connection_refused_is_a_certain_non_delivery_and_a_refusal_too(bridge):
    wa, make = bridge
    refusing = make("refuse")
    with pytest.raises(wa.BridgeError) as err:
        wa.send("34600000000", "hello")
    assert not isinstance(err.value, eo.OutcomeUnknownError)
    refusing.close()
    with pytest.raises(wa.BridgeError, match="not running"):
        wa.send("34600000000", "hello again")
    assert [r["effect_certainty"] for r in eo.list_effects()] == ["none", "none"]
    assert {r["state"] for r in eo.list_effects()} == {"failed_before_effect"}


def test_bridge_success_records_the_remote_id(bridge):
    wa, make = bridge
    make("ok")
    assert wa.send("34600000000", "hi")["id"] == "wa-1"
    row = eo.list_effects()[0]
    assert row["state"] == "succeeded" and row["external_ref"] == "wa-1"
