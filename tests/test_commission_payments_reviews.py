"""E29 follow-ups: payments received in several movements or in parts, and review desks filed under milestones."""
from __future__ import annotations

import datetime as dt

import pytest

from src import commission_office as co
from src import constants as constants_mod
from src import review_desk as rd


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def _ledger(monkeypatch, items):
    monkeypatch.setattr(co, "_hub_call", lambda app, tool, args: {"ok": True, "result": {"items": items}})


def _commission(amount=900.0, due=None):
    cid = co.run("alice", {"action": "create", "title": "Caja sensor", "client": "Taller Norte"})["id"]
    co.run("alice", {"action": "add_payment", "id": cid, "label": "Final", "amount": amount, "due": due or dt.date.today().isoformat()})
    return cid


def test_two_or_three_movements_that_add_up_are_offered_together(monkeypatch):
    today = dt.date.today().isoformat()
    _ledger(monkeypatch, [
        {"id": "a", "date": today, "amount_cents": 40000, "counterparty": "Taller Norte", "note": "parte 1"},
        {"id": "b", "date": today, "amount_cents": 50000, "counterparty": "Taller Norte", "note": "parte 2"},
        {"id": "c", "date": today, "amount_cents": 30000, "counterparty": "Otro", "note": ""},
        {"id": "d", "date": today, "amount_cents": 20000, "counterparty": "Otro", "note": ""},
        {"id": "e", "date": today, "amount_cents": -90000, "counterparty": "Taller Norte", "note": "gasto"}])
    cid = _commission()
    rec = co.run("alice", {"action": "reconcile_payments", "id": cid})
    cands = rec["payments"][0]["ledger_check"]["candidates"]
    combined = [c for c in cands if c["kind"] == "combined"]
    assert sorted(combined[0]["tx_ids"]) == ["a", "b"] and combined[0]["amount"] == 900.0       # same client ranks first
    assert any(sorted(c["tx_ids"]) == ["a", "c", "d"] for c in combined)                        # three movements also add up
    assert not any("e" in c["tx_ids"] for c in cands)                                            # outgoing never counts
    assert {c["tx_id"] for c in cands if c["kind"] == "partial"} == {"a", "b"}                   # smaller ones from the client
    rec = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Final", "tx_ids": ["a", "b"]})
    p = rec["payments"][0]
    assert p["confirmed"] is True and p["received"] == 900.0 and p["ledger_ref"] == "a, b"
    assert rec["status"]["payments_received"] == 900.0 and rec["status"]["payments_partly_paid"] == []


def test_partial_payments_accumulate_and_the_next_check_looks_for_what_is_still_owed(monkeypatch):
    today = dt.date.today().isoformat()
    items = [{"id": "p1", "date": today, "amount_cents": 30000, "counterparty": "Taller Norte", "note": "a cuenta"},
             {"id": "p2", "date": today, "amount_cents": 60000, "counterparty": "Taller Norte", "note": "resto"}]
    _ledger(monkeypatch, items[:1])
    cid = _commission()
    rec = co.run("alice", {"action": "reconcile_payments", "id": cid})
    assert [c["kind"] for c in rec["payments"][0]["ledger_check"]["candidates"]] == ["partial"]
    rec = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Final", "tx_id": "p1"})
    p = rec["payments"][0]
    assert p["confirmed"] is False and p["received"] == 300.0
    assert rec["status"]["payments_partly_paid"] == [{"label": "Final", "received": 300.0, "amount": 900.0}]
    assert rec["status"]["payments_received"] == 300.0
    _ledger(monkeypatch, items)
    rec = co.run("alice", {"action": "reconcile_payments", "id": cid})
    check = rec["payments"][0]["ledger_check"]
    assert check["owed"] == 600.0 and check["candidates"][0]["kind"] == "exact" and check["candidates"][0]["tx_id"] == "p2"
    assert not any("p1" in c["tx_ids"] for c in check["candidates"])                             # already recorded
    rec = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Final", "tx_id": "p2"})
    assert rec["payments"][0]["confirmed"] is True and rec["payments"][0]["received"] == 900.0
    assert "600 of 900" not in rec["events"][-1]["text"] and "paid" in rec["events"][-1]["text"]


def test_a_movement_cannot_pay_two_payments_and_an_unknown_one_keeps_the_old_behaviour(monkeypatch):
    _ledger(monkeypatch, [])
    cid = _commission()
    co.run("alice", {"action": "add_payment", "id": cid, "label": "Anticipo", "amount": 100})
    rec = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Anticipo", "tx_id": "zz"})
    assert rec["payments"][1]["confirmed"] is True and rec["payments"][1]["received"] == 100.0
    out = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Final", "tx_ids": "zz"})
    assert out["error_class"] == "office.bad_args" and "another payment" in out["error"]
    part = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Final", "tx_id": "q1", "amount": 250})
    assert part["payments"][0]["confirmed"] is False and part["payments"][0]["received"] == 250.0


def test_a_review_desk_filed_under_a_milestone_shows_its_state_and_its_delivery_reaches_the_commission(tmp_path):
    doc = tmp_path / "ficha.md"
    doc.write_text("# Ficha\nv1\n", encoding="utf-8")
    cid = co.run("alice", {"action": "create", "title": "Caja sensor"})["id"]
    co.run("alice", {"action": "add_milestone", "id": cid, "title": "Ficha técnica"})
    desk = rd.run("alice", {"action": "create", "title": "Ficha técnica", "path": str(doc)}, confine=False)
    rec = co.run("alice", {"action": "link_review", "id": cid, "milestone": "M1", "review_id": desk["id"]})
    rv = rec["status"]["reviews"]
    assert rv == [{"id": desk["id"], "title": "Ficha técnica", "milestone": "M1", "latest": 1, "delivered": None, "open": 0, "versions": 1}]
    assert rd.run("alice", {"action": "show", "id": desk["id"]})["commission"] == cid
    rd.run("alice", {"action": "comment", "id": desk["id"], "text": "Falta la tolerancia en la línea 2"})
    doc.write_text("# Ficha\nv2 con tolerancia\n", encoding="utf-8")
    rd.run("alice", {"action": "add_version", "id": desk["id"], "path": str(doc)}, confine=False)
    assert co.run("alice", {"action": "show", "id": cid})["status"]["reviews"][0]["open"] == 1
    out = rd.run("alice", {"action": "deliver", "id": desk["id"], "version": 2, "force": True})
    assert out["delivered"]["version"] == 2
    rec = co.run("alice", {"action": "show", "id": cid})
    assert rec["status"]["reviews"][0]["delivered"] == 2 and rec["status"]["reviews"][0]["latest"] == 2
    last = rec["events"][-1]
    assert last["kind"] == "delivery" and "delivered v2" in last["text"] and "1 open comment" in last["text"]
    bad = co.run("alice", {"action": "link_review", "id": cid, "milestone": "M1", "review_id": "nope-000"})
    assert bad["error_class"] == "office.unknown_review"
    assert co.run("alice", {"action": "link_review", "id": cid, "milestone": "M9", "review_id": desk["id"]})["error_class"] == "office.not_found"


def test_delivering_a_free_desk_or_one_whose_commission_is_gone_never_fails(tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("uno", encoding="utf-8")
    free = rd.run("alice", {"action": "create", "title": "Suelto", "path": str(doc)}, confine=False)
    assert rd.run("alice", {"action": "deliver", "id": free["id"]})["delivered"]["version"] == 1
    orphan = rd.run("alice", {"action": "create", "title": "Huérfano", "path": str(doc), "commission": "gone-123", "milestone": "M1"}, confine=False)
    assert rd.run("alice", {"action": "deliver", "id": orphan["id"]})["delivered"]["version"] == 1
