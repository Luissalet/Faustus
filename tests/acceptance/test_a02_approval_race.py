"""A02 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A02): the SAME
decision, submitted by two concurrent clients against a SINGLE pending
approval, must leave exactly one row decided and give the loser a coherent
receipt — never a corrupted card, never two different "truths" about who
decided it, never an opaque failure.

Two mechanisms in this codebase decide approvals, so both are exercised for
real, through the actual module under test, not a mock of it:

* `src/approval_store.py` + `routes/approvals_routes.py` — the human-facing
  HTTP grant/deny card, gone through the real FastAPI route (`TestClient`),
  the real `approval_store.decide()` CAS, and a real SQLite file (two
  connections, not the shared in-memory default, because the race is a
  write from a second connection).
* `src/tool_approvals.py` — the in-memory exact-tool-call approval a worker
  `consume()`s exactly once, exercised via the typed `consume_with_reason`
  this lot adds.

Both races are forced with a `threading.Barrier` placed immediately in front
of the exact write each mechanism uses to decide the race, the same
technique `tests/test_approval_decision_races.py` and
`tests/test_approval_concurrent_consumption.py` already use — two threads
merely started close together are not a reliable race on a fast local
SQLite file; a barrier makes the interleaving certain instead of probable.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from routes.approvals_routes import setup_approvals_routes
from src import approval_store
from src.tool_approval_scopes import TASK_APPROVAL_DECISION
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action

PLAN = {
    "action": "deliver", "skill_id": "mail.send", "skill_version": "1.0.0",
    "backend": "docker_workspace", "recipients": ["ana@example.com"],
    "cost_units": 0, "secret_names": ["smtp"], "output_kinds": ["document"],
    "detail": "Send the September report to Ana.",
}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A real SQLite FILE (not the shared in-memory default): the race is a
    write from a second connection, and two connections sharing one
    in-memory database is not the same failure mode as two processes on a
    real deployment's file."""
    url = "sqlite:///" + (tmp_path / "approvals.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    built = FastAPI()
    built.include_router(setup_approvals_routes())
    yield built
    engine.dispose()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.mark.acceptance("A02")
def test_two_clients_granting_the_same_card_at_once_leave_one_winner_and_a_coherent_loser_receipt(
    app, monkeypatch,
):
    with TestClient(app) as setup_client:
        card_id = setup_client.post(
            "/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
        ).json()["approval"]["id"]

    # Force the interleaving at the exact point approval_store.decide()'s
    # CAS reads the row: both requests reach it, then both proceed. Without
    # this, two fast local SQLite calls from the SAME test process are not
    # reliably concurrent -- the second one would usually just find the row
    # already decided from the ordinary, uninteresting path.
    session_class = db_mod.SessionLocal.class_
    original_get = session_class.get
    gate = threading.Barrier(2)
    seen = threading.local()

    def simultaneous_get(self, *args, **kwargs):
        row = original_get(self, *args, **kwargs)
        if not getattr(seen, "waited", False):
            seen.waited = True
            gate.wait(timeout=5)
        return row

    monkeypatch.setattr(session_class, "get", simultaneous_get)

    # Two SEPARATE TestClient instances (each its own httpx transport/
    # portal), one per thread -- sharing a single TestClient across threads
    # serializes requests through its own internal transport lock, which
    # would silently turn this into the sequential case below instead of a
    # real race.
    def grant(by: str):
        with TestClient(app) as c:
            return c.post(f"/api/approvals/{card_id}/grant", json={"by": by})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(grant, who) for who in ("alice", "bob")]
        responses = [f.result(timeout=10) for f in futures]
    monkeypatch.setattr(session_class, "get", original_get)

    statuses = sorted(r.status_code for r in responses)
    # Exactly one 200 (the winner) and one 409 (the loser) -- never two 200s
    # (that would mean the card was decided twice) and never an opaque 5xx.
    assert statuses == [200, 409], [r.status_code for r in responses]

    winner_resp = next(r for r in responses if r.status_code == 200)
    loser_resp = next(r for r in responses if r.status_code == 409)
    winner = winner_resp.json()
    loser = loser_resp.json()

    assert winner["ok"] is True
    assert winner["approval"]["status"] == "granted"
    assert winner["approval"]["decided_by"] in ("alice", "bob")

    # The loser's body is not an opaque failure: it carries the SAME
    # decided_by/decided_at/status the winner got, so its client can show
    # "alice already granted this at 12:03" instead of a bare error.
    assert loser["ok"] is False
    assert loser["reason"] == "already_granted"
    assert loser["approval"]["decided_by"] == winner["approval"]["decided_by"]
    assert loser["approval"]["decided_at"] == winner["approval"]["decided_at"]
    assert loser["approval"]["status"] == "granted"

    # The store itself agrees with the winner's receipt -- no third "truth".
    assert approval_store.get(card_id).to_dict() == winner["approval"]


@pytest.mark.acceptance("A02")
def test_two_clients_denying_after_a_grant_both_get_the_granted_receipt(client):
    """Not a race this time -- a plain sequential double-decide -- because a
    409 that carries a stale or empty receipt would be just as broken as one
    produced by an actual race. `by` differs so a bug that let the second
    call overwrite the first would be visible in `decided_by`."""
    card_id = client.post(
        "/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
    ).json()["approval"]["id"]

    granted = client.post(f"/api/approvals/{card_id}/grant", json={"by": "alice"})
    assert granted.status_code == 200

    denied_late = client.post(f"/api/approvals/{card_id}/deny", json={"by": "bob"})
    assert denied_late.status_code == 409
    body = denied_late.json()
    assert body["reason"] == "already_granted"
    assert body["approval"]["decided_by"] == "alice"
    assert body["approval"]["status"] == "granted"


@pytest.mark.acceptance("A02")
def test_two_workers_consuming_the_same_exact_tool_approval_get_typed_outcomes():
    """The second mechanism: `src.tool_approvals.ToolApprovalStore`, the
    in-memory single-use card a worker `consume()`s before an exact,
    previously-sealed tool call. `consume()` itself stayed byte-for-byte
    backward compatible (still returns `ExactToolApproval | None`); this
    proves the typed `consume_with_reason` it now wraps tells the two
    concurrent callers apart instead of handing both an equally blank
    `None`.

    No barrier is needed here (unlike the HTTP race above): the whole
    consume is already inside one `threading.Lock`, so two threads
    submitting it concurrently deterministically produce one winner and one
    loser every run -- the interesting behavior under test is not WHETHER a
    race is possible (the lock already rules that out) but WHAT the loser
    is told, which used to be an indistinguishable `None`.
    """
    store = ToolApprovalStore()
    caps = capabilities_for_action("write_file", '{"path": "x", "content": "y"}')
    pending = store.create(
        owner="luis", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x", "content": "y"}', workspace="",
        external_untrusted_context_seen=True, capabilities=caps,
    )

    def consume():
        return store.consume_with_reason(
            pending.approval_id, decision=TASK_APPROVAL_DECISION,
            owner="luis", session_id="s1",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume) for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]

    reasons = sorted(reason for reason, _approval in results)
    assert reasons == ["already_consumed", "consumed"], results

    winner_reason, winner_approval = next(r for r in results if r[0] == "consumed")
    loser_reason, loser_approval = next(r for r in results if r[0] == "already_consumed")
    assert winner_approval is not None and winner_approval.pending.approval_id == pending.approval_id
    assert loser_approval is None

    # consume() (the pre-existing, unchanged-signature wrapper) still
    # behaves exactly like before: None for the loser, the approval for the
    # winner -- proven independently of consume_with_reason's own locking.
    store2 = ToolApprovalStore()
    pending2 = store2.create(
        owner="luis", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x", "content": "y"}', workspace="",
        external_untrusted_context_seen=True, capabilities=caps,
    )
    first = store2.consume(pending2.approval_id, decision=TASK_APPROVAL_DECISION,
                            owner="luis", session_id="s1")
    second = store2.consume(pending2.approval_id, decision=TASK_APPROVAL_DECISION,
                             owner="luis", session_id="s1")
    assert first is not None and second is None
