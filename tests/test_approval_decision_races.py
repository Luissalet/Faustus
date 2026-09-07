"""Concurrent human decisions must preserve the first committed answer."""
from concurrent.futures import ThreadPoolExecutor
import threading

from core import database as db_mod
from src import approval_store
from tests.test_approval_store import store, PLAN  # noqa: F401


def test_two_people_cannot_overwrite_the_same_pending_answer(store, monkeypatch):
    card = approval_store.request(PLAN, owner="alice")
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approval_store.decide, card.id, granted=answer,
                               by=who, reason=who)
                   for answer, who in [(True, "alice"), (False, "bob")]]
        results = [f.result(timeout=10) for f in futures]
    monkeypatch.setattr(session_class, "get", original_get)
    assert sum(r["ok"] for r in results) == 1
    winner = next(r for r in results if r["ok"])["approval"]
    loser = next(r for r in results if not r["ok"])
    assert loser["reason"] == "already_" + winner["status"]
    assert approval_store.get(card.id).to_dict() == winner


def test_a_late_grant_cannot_resurrect_an_expired_card(store, monkeypatch):
    card = approval_store.request(PLAN, owner="alice", ttl_seconds=60)
    reached = threading.Event()
    release = threading.Event()
    original_now = approval_store.now_iso

    def held_now():
        reached.set()
        assert release.wait(timeout=5)
        return original_now()

    monkeypatch.setattr(approval_store, "now_iso", held_now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        decision = pool.submit(approval_store.decide, card.id, granted=True, by="alice")
        try:
            assert reached.wait(timeout=5)
            assert approval_store.expire_stale(now="2099-01-01T00:00:00Z") == 1
        finally:
            release.set()
        result = decision.result(timeout=10)
    assert not result["ok"]
    assert approval_store.get(card.id).status == "expired"


def test_exact_plan_is_not_permission_to_borrow_another_owners_card(store):
    card = approval_store.request(PLAN, owner="alice")
    approval_store.decide(card.id, granted=True, by="alice")
    assert approval_store.check(PLAN, owner="alice")["ok"]
    for owner in ("bob", ""):
        result = approval_store.check(PLAN, owner=owner)
        assert not result["ok"]
        assert result["approval_id"] == ""
        assert result["changes"] == []
