"""Commission office (Hangar E29, first cut): scope -> milestone -> delivery,
with scope versions compared by the scope comparator and deliverables pointing
at delivery packages whose readiness is read live."""
from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

import pytest

from src import commission_office as co
from src import constants as constants_mod
from src import delivery_package as dp


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(dp, "_confine", lambda p: Path(str(p)).resolve())
    yield


def _stl(path: Path) -> Path:
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12f", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0) + b"\0\0")
    return path


def test_cycle_scope_milestone_delivery(tmp_path):
    rec = co.run("alice", {"action": "create", "title": "Caja para sensor", "client": "Taller Norte",
                           "request": "Diseñar una caja para el sensor.\nEntregar planos en PDF.",
                           "budget": {"amount": 1200, "currency": "EUR"}})
    cid = rec["id"]
    assert rec["stage"] == "opportunity" and rec["scope"][0]["kind"] == "initial" and "comparison" not in rec["scope"][0]
    rec = co.run("alice", {"action": "add_scope", "id": cid, "kind": "agreement",
                           "text": "Diseñar una caja para el sensor.\nEntregar planos en PDF.\nPlazo de entrega de 10 días."})
    assert rec["stage"] == "agreed" and rec["scope"][1]["comparison"]["counts"]["added"] == 1
    rec = co.run("alice", {"action": "add_scope", "id": cid, "kind": "change",
                           "text": "Entregar planos en DOCX.\nQue quede más bonito."})
    cmp = rec["scope"][2]["comparison"]
    assert cmp["against"] == 2 and cmp["counts"]["changed"] == 1 and cmp["counts"]["ambiguous"] == 1
    assert rec["status"]["pending_change"] is True and len(rec["status"]["open_questions"]) == 1
    past = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    rec = co.run("alice", {"action": "add_milestone", "id": cid, "title": "Primera entrega", "due": past,
                           "deliverables": ["Caja STL"], "amount": 600})
    assert rec["status"]["overdue"] == ["M1"] and rec["status"]["deliverables_without_ready_package"] == ["Caja STL"]
    pkg = dp.run("alice", {"name": "Caja", "items": [{"path": str(_stl(tmp_path / "caja.stl")), "role": "editable"}],
                           "instructions": "Abrir", "limitations": ["Ejemplo"]})
    rec = co.run("alice", {"action": "link_package", "id": cid, "milestone": "M1", "package_id": pkg["id"], "deliverable": "Caja STL"})
    assert rec["status"]["deliverables"][0]["ready"] is True and rec["status"]["deliverables_without_ready_package"] == []
    rec = co.run("alice", {"action": "update_milestone", "id": cid, "milestone": "M1", "done": True})
    assert rec["status"]["overdue"] == [] and rec["status"]["milestones_done"] == 1 and rec["status"]["billing_expected"] == 600.0
    kinds = [e["kind"] for e in rec["events"]]
    assert kinds[:3] == ["created", "scope", "stage"] and "delivery" in kinds


def test_contacts_files_decisions_payments_and_list():
    rec = co.run("alice", {"action": "create", "title": "Web de reservas"})
    cid = rec["id"]
    co.run("alice", {"action": "add_contact", "id": cid, "name": "Ana", "role": "cliente", "people_id": "p-12"})
    co.run("alice", {"action": "add_file", "id": cid, "path": "E:/encargos/web/brief.pdf", "role": "brief"})
    co.run("alice", {"action": "add_decision", "id": cid, "text": "Pagos con transferencia, sin pasarela", "date": "2026-11-04"})
    rec = co.run("alice", {"action": "add_payment", "id": cid, "label": "Anticipo 30 %", "amount": 450, "ledger_ref": "L-88"})
    assert rec["contacts"][0]["people_id"] == "p-12" and rec["files"][0]["role"] == "brief"
    assert rec["decisions"][0]["date"] == "2026-11-04" and rec["payments"][0]["ledger_ref"] == "L-88"
    rows = co.run("alice", {"action": "list"})["commissions"]
    assert rows[0]["id"] == cid and co.run("bob", {"action": "list"})["commissions"] == []
    rec = co.run("alice", {"action": "update", "id": cid, "stage": "in_progress", "budget": {"amount": 1500}})
    assert rec["stage"] == "in_progress" and rec["budget"]["currency"] == "EUR"


def test_errors_are_results_not_exceptions():
    assert co.run("alice", {"action": "create"})["error_class"] == "office.bad_args"
    assert co.run("alice", {"action": "show", "id": "../x"})["error_class"] == "office.not_found"
    cid = co.run("alice", {"action": "create", "title": "X"})["id"]
    assert co.run("alice", {"action": "add_milestone", "id": cid, "title": "a", "due": "mañana"})["error_class"] == "office.bad_date"
    assert co.run("alice", {"action": "update", "id": cid, "stage": "nope"})["error_class"] == "office.bad_stage"
    assert co.run("alice", {"action": "link_package", "id": cid, "milestone": "M9", "package_id": "x"})["error_class"] == "office.not_found"
    co.run("alice", {"action": "add_milestone", "id": cid, "title": "a"})
    assert co.run("alice", {"action": "link_package", "id": cid, "milestone": "M1", "package_id": "20260101-000000-abcdef"})["error_class"] == "office.unknown_package"
    assert co.run("alice", {"action": "zzz"})["error_class"] == "office.bad_args"
