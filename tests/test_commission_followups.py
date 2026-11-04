"""E29 follow-ups: People looked up and Ledger reconciled live through the Hub,
read-only; only ids are stored in the commission."""
from __future__ import annotations

import datetime as dt

import pytest

from src import commission_office as co
from src import constants as constants_mod


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def test_people_lookup_and_link(monkeypatch):
    calls = []

    def fake(app, tool, args):
        calls.append((app, tool, dict(args)))
        return {"ok": True, "result": {"results": [{"id": "p-1", "name": "Ana Ruiz", "score": 0.92}, {"name": "sin id"}]}}
    monkeypatch.setattr(co, "_hub_call", fake)
    found = co.run("alice", {"action": "people_lookup", "name": "Ana"})
    assert found["people"] == [{"id": "p-1", "name": "Ana Ruiz", "score": 0.92, "relation": ""}]
    assert calls == [("people", "find_people", {"query": "Ana", "limit": 6})]
    cid = co.run("alice", {"action": "create", "title": "Caja"})["id"]
    rec = co.run("alice", {"action": "link_person", "id": cid, "name": "Ana Ruiz", "people_id": "p-1", "role": "cliente"})
    assert rec["contacts"][0]["people_id"] == "p-1" and rec["status"]["people_linked"] == 1


def test_people_lookup_reports_a_down_hub(monkeypatch):
    monkeypatch.setattr(co, "_hub_call", lambda *a: {"ok": False, "error": "hub not reachable"})
    out = co.run("alice", {"action": "people_lookup", "name": "Ana"})
    assert out["people"] == [] and "hub" in out["error"]


def test_reconcile_lists_exact_incoming_amounts_and_confirm_records_the_id(monkeypatch):
    due = dt.date.today().isoformat()
    entries = {"items": [
        {"id": "t1", "date": due, "amount_cents": 45000, "counterparty": "Taller Norte", "note": "Anticipo caja"},
        {"id": "t2", "date": "2020-01-01", "amount_cents": 45000, "counterparty": "Otro", "note": ""},
        {"id": "t3", "date": due, "amount_cents": -45000, "counterparty": "Taller Norte", "note": "gasto"},
        {"id": "t4", "date": due, "amount_cents": 45100, "counterparty": "Taller Norte", "note": ""}]}
    seen = []
    monkeypatch.setattr(co, "_hub_call", lambda app, tool, args: (seen.append((app, tool)), {"ok": True, "result": entries})[1])
    cid = co.run("alice", {"action": "create", "title": "Caja", "client": "Taller Norte"})["id"]
    co.run("alice", {"action": "add_payment", "id": cid, "label": "Anticipo 30 %", "amount": 450, "due": due})
    rec = co.run("alice", {"action": "reconcile_payments", "id": cid})
    cands = rec["payments"][0]["ledger_check"]["candidates"]
    assert [c["tx_id"] for c in cands] == ["t1", "t2"] and cands[0]["score"] == 1.0 and cands[1]["score"] == 0.6
    assert seen == [("ledger", "list_entries")] and rec["status"]["payments_to_confirm"] == ["Anticipo 30 %"]
    rec = co.run("alice", {"action": "confirm_payment", "id": cid, "label": "Anticipo 30 %", "tx_id": "t1"})
    assert rec["payments"][0]["confirmed"] is True and rec["payments"][0]["ledger_ref"] == "t1"
    assert rec["status"]["payments_received"] == 450.0 and rec["status"]["payments_to_confirm"] == []
    assert co.run("alice", {"action": "confirm_payment", "id": cid, "label": "x", "tx_id": "t1"})["error_class"] == "office.not_found"


def test_reconcile_with_the_ledger_down_keeps_the_error(monkeypatch):
    monkeypatch.setattr(co, "_hub_call", lambda *a: {"ok": False, "error": "hub not reachable"})
    cid = co.run("alice", {"action": "create", "title": "Caja"})["id"]
    co.run("alice", {"action": "add_payment", "id": cid, "label": "Final", "amount": 100})
    rec = co.run("alice", {"action": "reconcile_payments", "id": cid})
    assert rec["reconcile"]["error"] == "hub not reachable" and rec["payments"][0]["ledger_check"]["error"]


def test_a_full_ledger_page_is_reported_as_truncated(monkeypatch):
    rows = [{"id": f"t{i}", "amount_cents": 100, "date": "2026-11-01", "counterparty": "x"} for i in range(co.LEDGER_PAGE)]
    monkeypatch.setattr(co, "_hub_call", lambda app, tool, args: {"ok": True, "result": {"items": rows}})
    rec = {"id": "c", "client": "", "events": [], "payments": [{"label": "Anticipo", "amount": 360.0, "due": "2026-11-10"}]}
    out = co.reconcile_payments(rec)
    assert out["truncated"] is True and rec["payments"][0]["ledger_check"]["truncated"] is True
    assert "proves nothing" in rec["events"][-1]["text"]
