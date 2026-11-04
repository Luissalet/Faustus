"""Proposal from a commission (Hangar E31): every point says where it comes
from, internal names and paths are masked and listed, nothing is invented."""
from __future__ import annotations

import pytest

from src import commission_office as co
from src import constants as constants_mod
from src import proposal_builder as pb


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def _commission():
    cid = co.run("alice", {"action": "create", "title": "Caja para sensor", "client": "Taller Norte",
                           "request": "Diseñar una caja para el sensor.\nEntregar planos en PDF."})["id"]
    co.run("alice", {"action": "add_scope", "id": cid, "kind": "agreement",
                     "text": "Diseñar una caja para el sensor.\nEntregar planos en PDF.\nPlazo de entrega de 10 días."})
    co.run("alice", {"action": "add_scope", "id": cid, "kind": "change", "text": "Entregar planos en DOCX.\nQue quede más bonito."})
    co.run("alice", {"action": "add_milestone", "id": cid, "title": "Primera entrega", "due": "2026-12-01", "amount": 600})
    co.run("alice", {"action": "add_contact", "id": cid, "name": "Ana", "email": "ana@taller.example"})
    co.run("alice", {"action": "add_decision", "id": cid, "date": "2026-11-04",
                     "text": "Modelo revisado en Vulcan con E:\\encargos\\caja\\caja.stl; dudas a ana@taller.example o a soporte@interno.example"})
    return cid


def test_proposal_sections_origins_and_masking():
    cid = _commission()
    out = co.run("alice", {"action": "proposal", "id": cid, "assumptions": "El cliente aporta el sensor.",
                           "exclusions": "Fabricación de la caja", "internal_terms": "Taller interno"})
    prop = out["proposal"]
    titles = [s["title"] for s in prop["sections"]]
    assert titles[:4] == ["Contexto", "Alcance acordado", "Cambios solicitados pendientes de acordar", "Plan e hitos"]
    origins = {s["title"]: {it["origin"] for it in s["items"]} for s in prop["sections"] if s["items"]}
    assert origins["Alcance acordado"] == {"verified"} and origins["Supuestos"] == {"assumption"}
    assert origins["Cambios solicitados pendientes de acordar"] == {"question"}
    assert any("bonito" in it["text"] for s in prop["sections"] if s["title"] == "Preguntas abiertas" for it in s["items"])
    decision = next(s for s in prop["sections"] if s["title"] == "Decisiones tomadas")["items"][0]["text"]
    assert "Vulcan" not in decision and "E:\\" not in decision and "soporte@interno.example" not in decision
    assert "ana@taller.example" in decision
    kinds = {l["kind"] for l in prop["leaks"]}
    assert kinds == {"internal name", "path", "email"}
    assert out["stage"] == "agreed" and out["proposals"][0]["name"] == "propuesta-1"
    from docx import Document
    doc = Document(prop["files"]["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Propuesta: Caja para sensor" in text and "[verificado]" in text and "Vulcan" not in text


def test_empty_sections_say_so_and_files_are_served_by_name():
    cid = co.run("alice", {"action": "create", "title": "Web"})["id"]
    out = co.run("alice", {"action": "proposal", "id": cid, "lang": "en"})
    assert out["stage"] == "proposal"
    md = pb.proposal_file("alice", cid, "propuesta-1", "md").read_text(encoding="utf-8")
    assert "No data in the record." in md
    with pytest.raises(FileNotFoundError):
        pb.proposal_file("alice", cid, "../x", "md")
    with pytest.raises(FileNotFoundError):
        pb.proposal_file("alice", cid, "propuesta-1", "exe")
