"""Scope comparator (Hangar E30): included / changed / added / removed /
ambiguous clauses, effects grounded only in the decision map, effort only from
given ranges. The proposal's test: a trivial format change and a change that
reruns the pipeline get different effects, each with its links as reason."""
from __future__ import annotations

import pytest

from src import constants as constants_mod
from src import derivation_graph as dg
from src import scope_compare as sc

AGREEMENT = """
- Entregar el informe final exportado en PDF.
- Medidas de las piezas en milímetros.
- Incluir tres vistas renderizadas de la pieza.
- Plazo de entrega de 10 días.
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture()
def graph():
    """ingest -> clean -> model -> render -> export_pdf; 'PDF' feeds the export
    only, 'milímetros' feeds the ingest (the whole pipeline)."""
    prev = None
    for n in ("ingest", "clean", "model", "render", "export_pdf"):
        dg.define_node("alice", "pieza", n, op={"name": n, "version": "1"},
                       inputs=[{"node": prev}] if prev else [], outputs=[f"{n}.out"])
        prev = n
    dg.map_add("alice", "pieza", "fmt:pdf", "format", "PDF")
    dg.map_add("alice", "pieza", "unit:mm", "unit", "milímetros")
    dg.map_add("alice", "pieza", "deliv:informe", "deliverable", "Informe final")
    dg.map_link("alice", "pieza", "fmt:pdf", "node:export_pdf")
    dg.map_link("alice", "pieza", "unit:mm", "node:ingest")
    dg.map_link("alice", "pieza", "node:export_pdf", "deliv:informe")
    return "pieza"


def by_clause(res):
    return {r["clause"]: r for r in res["rows"]}


def test_format_change_is_local_and_unit_change_reruns_the_pipeline(graph):
    res = sc.run("alice", {"agreement": AGREEMENT, "graph": graph,
                           "new_request": "Entregar el informe final exportado en DOCX.\nMedidas de las piezas en pulgadas.",
                           "effort_hours": {"export_pdf": [1, 2], "ingest": [2, 4], "clean": [1, 3], "model": [4, 8],
                                            "render": [2, 3]}})
    rows = by_clause(res)
    fmt = rows["Entregar el informe final exportado en DOCX"]
    unit = rows["Medidas de las piezas en pulgadas"]
    assert fmt["status"] == "changed" and fmt["diff"]["attributes"] == {"from": ["pdf"], "to": ["docx"]}
    assert fmt["effect"]["class"] == "local" and fmt["effect"]["recalculate"] == ["export_pdf"]
    assert fmt["effect"]["effort_hours"] == [1.0, 2.0] and "Informe final" in fmt["effect"]["deliverables"]
    assert unit["status"] == "changed" and unit["effect"]["class"] == "pipeline"
    assert unit["effect"]["recalculate"] == ["ingest", "clean", "model", "render", "export_pdf"]
    assert unit["effect"]["effort_hours"] == [10.0, 20.0]
    assert any("Asumirlo" in o for o in fmt["options"]) and any("Orden de cambio de 10.0–20.0 h" in o for o in unit["options"])


def test_included_added_removed_and_ambiguous(graph):
    new = ("Incluir tres vistas renderizadas de la pieza.\nAñadir una animación del montaje.\n"
           "Ya no hace falta el plazo de 10 días.\nQue el visor quede más bonito.")
    res = sc.run("alice", {"agreement": AGREEMENT, "new_request": new, "graph": graph})
    rows = by_clause(res)
    assert rows["Incluir tres vistas renderizadas de la pieza"]["status"] == "included"
    added = rows["Añadir una animación del montaje"]
    assert added["status"] == "added" and added["effect"]["class"] == "unknown"
    assert any("mapa de decisiones" in o for o in added["options"])
    assert rows["Ya no hace falta el plazo de 10 días"]["status"] == "removed"
    vague = rows["Que el visor quede más bonito"]
    assert vague["status"] == "ambiguous" and "bonito" in vague["question"]
    assert res["counts"] == {"included": 1, "changed": 0, "added": 1, "removed": 1, "ambiguous": 1}


def test_without_a_map_effects_are_unknown_and_no_effort_is_invented():
    res = sc.run("alice", {"agreement": AGREEMENT, "new_request": "Entregar el informe final exportado en DOCX.",
                           "effort_hours": {"export_pdf": [1, 2]}})
    row = res["rows"][0]
    assert row["status"] == "changed" and row["effect"]["class"] == "unknown" and "effort_hours" not in row["effect"]


def test_replace_mode_lists_what_the_new_request_no_longer_mentions():
    res = sc.run("alice", {"agreement": AGREEMENT, "mode": "replace",
                           "new_request": "Entregar el informe final exportado en PDF.\nIncluir tres vistas renderizadas de la pieza."})
    removed = [r["clause"] for r in res["rows"] if r["status"] == "removed"]
    assert removed == ["Medidas de las piezas en milímetros", "Plazo de entrega de 10 días"]


def test_quantity_change_and_negotiated_changes_from_the_initial_request():
    res = sc.run("alice", {"initial": "Plazo de entrega de 15 días.\nEntregar el informe final exportado en PDF.",
                           "agreement": AGREEMENT, "new_request": "Plazo de entrega de 7 días.", "lang": "en"})
    row = res["rows"][0]
    assert row["status"] == "changed" and row["diff"]["quantities"]["dia"] == {"from": "10", "to": "7"}
    assert any(n["status"] == "changed" and "10" in n["clause"] for n in res["negotiated"])
    assert any(n["status"] == "added" for n in res["negotiated"])


def test_partial_effort_ranges_say_which_nodes_have_none(graph):
    res = sc.run("alice", {"agreement": AGREEMENT, "graph": graph, "new_request": "Medidas de las piezas en pulgadas.",
                           "effort_hours": {"ingest": [2, 4]}})
    eff = res["rows"][0]["effect"]
    assert eff["effort_hours"] == [2.0, 4.0] and eff["no_effort_for"] == ["clean", "model", "render", "export_pdf"]
    assert "sin rango para" in " ".join(res["rows"][0]["options"])


def test_list_show_and_errors():
    res = sc.run("alice", {"agreement": AGREEMENT, "new_request": "Añadir una animación del montaje.", "title": "Cambio 1"})
    assert sc.run("alice", {"action": "list"})["comparisons"][0]["title"] == "Cambio 1"
    assert sc.run("bob", {"action": "list"})["comparisons"] == []
    assert sc.run("alice", {"action": "show", "id": res["id"]})["rows"][0]["status"] == "added"
    assert sc.run("alice", {"action": "show", "id": "../x"})["error_class"] == "scope.not_found"
    assert sc.run("alice", {"agreement": "x"})["error_class"] == "scope.bad_args"
    assert sc.run("alice", {"action": "zzz"})["error_class"] == "scope.bad_args"


def test_a_clause_that_adds_a_new_item_is_not_already_included():
    base = "Entregar informe en PDF."
    rows = sc.run("u", {"action": "compare", "agreement": base, "new_request": "Entregar el informe con gráficos."})["rows"]
    assert rows[0]["status"] == "ambiguous" and rows[0]["reason"] == "partial"
    same = sc.run("u", {"action": "compare", "agreement": base, "new_request": "Entregar el informe en PDF."})["rows"]
    assert same[0]["status"] == "included"


def test_without_and_drop_are_not_removals():
    rows = sc.run("u", {"action": "compare", "agreement": "Entregar el prototipo de la caja.",
                        "new_request": "Entregar el prototipo de la caja without scratches."})["rows"]
    assert rows[0]["status"] != "removed"

def test_two_comparisons_of_the_same_texts_in_one_second_do_not_overwrite_each_other():
    args = {"action": "compare", "agreement": "Entregar informe en PDF.", "new_request": "Entregar informe en DOCX."}
    a, b = sc.run("u", args), sc.run("u", args)
    assert a["id"] != b["id"] and len(sc.run("u", {"action": "list"})["comparisons"]) == 2
