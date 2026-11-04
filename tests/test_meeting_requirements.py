"""Meeting to requirements (Hangar E27): conditional wording is never a
commitment; every item keeps speaker, time and line; promotion is explicit."""
from __future__ import annotations

import pytest

from src import commission_office as co
from src import constants as constants_mod
from src import meeting_requirements as mr

MEETING = """## Transcript
[00:01:05] Ana: Necesitamos que la caja admita un conector USB-C.
[00:02:10] Luis: Podríamos hacer la tapa transparente, pero encarece.
[00:03:30] Ana: ¿La entrega puede ser antes del día 20?
[00:04:02] Luis: Te envío el render el viernes.
[00:05:15] Ana: Decidimos usar PETG para la caja.
[00:06:00] Luis: Hace buen día hoy.
- Ana: Quizá también una versión en negro.
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def test_extract_kinds_speakers_and_times():
    items = mr.extract(MEETING)
    kinds = [(i["kind"], i["speaker"], i["time"]) for i in items]
    assert kinds == [("requirement", "Ana", "00:01:05"), ("proposal", "Luis", "00:02:10"), ("question", "Ana", "00:03:30"),
                     ("promise", "Luis", "00:04:02"), ("decision", "Ana", "00:05:15"), ("proposal", "Ana", "")]
    assert all(not i["firm"] for i in items if i["kind"] in ("proposal", "question"))
    assert mr.counts(items) == {"decision": 1, "promise": 1, "requirement": 1, "proposal": 2, "question": 1}


def test_conditional_never_becomes_a_commitment():
    assert mr.classify_line("Podríamos enviar el render el viernes") == "proposal"
    assert mr.classify_line("We could deliver it next week") == "proposal"
    assert mr.classify_line("I'll deliver it next week") == "promise"
    assert mr.classify_line("The part must be printed in PETG") == "requirement"


def test_commission_imports_a_meeting_and_promotes_only_on_request():
    cid = co.run("alice", {"action": "create", "title": "Caja", "request": "Diseñar una caja para el sensor."})["id"]
    co.run("alice", {"action": "add_scope", "id": cid, "kind": "agreement", "text": "Diseñar una caja para el sensor."})
    rec = co.run("alice", {"action": "import_meeting", "id": cid, "text": MEETING, "source": "reunion-2026-11-04"})
    m = rec["meetings"][0]
    assert m["id"] == "R1" and m["counts"]["proposal"] == 2 and len(rec["scope"]) == 2 and rec["decisions"] == []
    assert rec["status"]["meeting_items_pending"] == 6
    rec = co.run("alice", {"action": "promote_meeting_item", "id": cid, "meeting": "R1", "n": 1, "as": "requirement"})
    assert rec["scope"][-1]["kind"] == "change" and "USB-C" in rec["scope"][-1]["text"]
    assert rec["scope"][-1]["source"].startswith("R1#1 Ana 00:01:05")
    rec = co.run("alice", {"action": "promote_meeting_item", "id": cid, "meeting": "R1", "n": 5, "as": "decision"})
    assert rec["decisions"][-1]["text"].startswith("Decidimos usar PETG") and rec["decisions"][-1]["by"] == "Ana"
    rec = co.run("alice", {"action": "promote_meeting_item", "id": cid, "meeting": "R1", "n": 4, "as": "milestone"})
    assert rec["milestones"][-1]["title"].startswith("Te envío el render")
    bad = co.run("alice", {"action": "promote_meeting_item", "id": cid, "meeting": "R1", "n": 2, "as": "requirement"})
    assert bad["error_class"] == "office.not_firm"
    assert co.run("alice", {"action": "promote_meeting_item", "id": cid, "meeting": "R1", "n": 2, "as": "requirement", "force": True})["scope"][-1]["kind"] == "change"
    assert rec["status"]["meeting_items_pending"] == 3


def test_common_spanish_obligations_are_requirements():
    from src import meeting_requirements as mr
    assert mr.classify_line("Hay que entregar en PDF") == "requirement"
    assert mr.classify_line("El cliente necesita el plano en DWG.") == "requirement"
    assert mr.classify_line("Hay que entregar en PDF, ¿no?") == "question"
    assert mr.classify_line("Quizá hay que entregar en PDF") == "proposal"
