"""The Cerebro as JSON Canvas 1.0 — export, import, round trips, undo.

Every test runs against its own brain directory (``brain_db.use_dir``), so
the vault and brain.db are disposable.
"""
from __future__ import annotations

import json
import os

import pytest

from src.brain import canvas, db as brain_db, notes, vault

OWNER = "ada"


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    yield
    brain_db.use_dir(None)


def _note(title, body, folder="Notes", owner=OWNER):
    return notes.create_note(owner, title, folder=folder, content=body)


def _seed(owner=OWNER):
    _note("Faustus", "Agente local. Usa [[Ollama]] y escribe en el [[Cerebro]]. #proyecto")
    _note("Ollama", "Servidor de modelos. Lo usa [[Faustus]].")
    _note("Cerebro", "Grafo de notas. Ver [[Obsidian]] y [[Faustus|el agente]].")
    _note("Suelta", "Nota sin enlaces.")


def _boxes_overlap(a, b):
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def _graph_signature(owner):
    g = notes.graph(owner, limit=10000)
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    return ({n["label"] for n in g["nodes"]},
            {(labels[e["from"]], labels[e["to"]]) for e in g["edges"]})


# ── validation ───────────────────────────────────────────────────────────

def test_validate_accepts_the_spec_sample_and_rejects_broken_documents():
    sample = {
        "nodes": [
            {"id": "g1", "type": "group", "x": -20, "y": -20, "width": 600, "height": 300, "label": "Ideas"},
            {"id": "t1", "type": "text", "x": 0, "y": 0, "width": 250, "height": 60, "text": "# Hola", "color": "4"},
            {"id": "f1", "type": "file", "x": 300, "y": 0, "width": 250, "height": 60, "file": "Notes/A.md", "subpath": "#Intro"},
            {"id": "l1", "type": "link", "x": 0, "y": 400, "width": 250, "height": 60, "url": "https://jsoncanvas.org", "color": "#ff0000"},
        ],
        "edges": [{"id": "e1", "fromNode": "t1", "fromSide": "right", "toNode": "f1", "toSide": "left", "label": "ve"}],
    }
    assert canvas.validate(sample) == []
    assert canvas.validate({}) == []
    broken = {
        "nodes": [
            {"id": "a", "type": "text", "x": 0.5, "y": 0, "width": 10, "height": 10},
            {"id": "a", "type": "shape", "x": 0, "y": 0, "width": 10, "height": 10, "color": "9"},
            {"id": "f", "type": "file", "x": 0, "y": 0, "width": 10, "height": 10, "file": "x.md", "subpath": "Intro"},
        ],
        "edges": [{"id": "f", "fromNode": "a", "toNode": "zz", "fromSide": "middle", "toEnd": "dot"}],
    }
    errors = " | ".join(canvas.validate(broken))
    for fragment in ("`x` must be an integer", "needs a string `text`", "duplicate id 'a'", "`type` must be one of",
                     "`color` must be", "`subpath` must", "duplicate id 'f'", "'zz' is not a node", "`fromSide`", "`toEnd`"):
        assert fragment in errors, fragment


def test_load_rounds_float_geometry_and_reads_json_text():
    doc = canvas.load('{"nodes":[{"id":"a","type":"text","text":"x","x":1.6,"y":-2.4,"width":100.0,"height":50}]}')
    node = doc["nodes"][0]
    assert (node["x"], node["y"], node["width"]) == (2, -2, 100)
    with pytest.raises(ValueError):
        canvas.load("not json")


# ── export ───────────────────────────────────────────────────────────────

def test_export_is_valid_deterministic_and_without_overlaps():
    _seed()
    first = canvas.export_canvas(OWNER)
    second = canvas.export_canvas(OWNER)
    assert first["ok"] and first["errors"] == []
    assert first["canvas"] == second["canvas"]
    doc = first["canvas"]
    items = [n for n in doc["nodes"] if n["type"] != "group"]
    groups = [n for n in doc["nodes"] if n["type"] == "group"]
    assert {n.get("file") for n in items if n["type"] == "file"} == {
        "Notes/Faustus.md", "Notes/Ollama.md", "Notes/Cerebro.md", "Notes/Suelta.md"}
    assert any(n["type"] == "text" and n["text"] == "[[Obsidian]]" and n["color"] == "1" for n in items)
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            assert not _boxes_overlap(a, b), (a["id"], b["id"])
    # every node sits inside the group of its kind; groups come first (lowest z)
    assert doc["nodes"][:len(groups)] == groups
    for node in items:
        assert any(canvas._inside(node, g) for g in groups)
    labels = {e.get("label") for e in doc["edges"]}
    assert "el agente" in labels
    assert all(e["fromSide"] in canvas.SIDES and e["toSide"] in canvas.SIDES for e in doc["edges"])
    assert first["stats"]["edges"] == 5


def test_export_subgraph_by_centre_depth_and_filters():
    _seed()
    one = canvas.export_canvas(OWNER, center="Ollama", depth=1)
    files = {n.get("file") for n in one["canvas"]["nodes"] if n["type"] == "file"}
    assert files == {"Notes/Ollama.md", "Notes/Faustus.md"}
    assert one["layout"] == "layers" and one["group_by"] == "none"
    two = canvas.export_canvas(OWNER, center="Notes/Ollama.md", depth=2, include_unresolved=False)
    assert {n.get("file") for n in two["canvas"]["nodes"]} >= {"Notes/Cerebro.md"}
    assert not any(n["type"] == "text" for n in two["canvas"]["nodes"])
    tagged = canvas.export_canvas(OWNER, tags=["proyecto"])
    assert [n.get("file") for n in tagged["canvas"]["nodes"] if n["type"] == "file"] == ["Notes/Faustus.md"]
    with pytest.raises(ValueError):
        canvas.export_canvas(OWNER, center="No existe")
    capped = canvas.export_canvas(OWNER, limit=2)
    assert capped["truncated"] and capped["stats"]["nodes"] == 2


def test_export_saves_inside_the_vault_and_to_a_chosen_path(tmp_path):
    _seed()
    out = canvas.export_canvas(OWNER, save=True, name="Mapa", save_path=str(tmp_path))
    assert out["path"] == "Canvas/Mapa.canvas"
    inside = os.path.join(vault.vault_root(OWNER), "Canvas", "Mapa.canvas")
    assert json.load(open(inside, encoding="utf-8")) == out["canvas"]
    assert os.path.exists(tmp_path / "Mapa.canvas")
    # a .canvas file in the vault is not a note
    assert all(not n["path"].endswith(".canvas") for n in notes.tree(OWNER)["notes"])


# ── import ───────────────────────────────────────────────────────────────

EXTERNAL = {
    "nodes": [
        {"id": "grp", "type": "group", "x": -40, "y": -80, "width": 900, "height": 400, "label": "Ideas clave"},
        {"id": "a1", "type": "text", "x": 0, "y": 0, "width": 260, "height": 120,
         "text": "# Pez y elefante\n\nRevisar con contexto limpio.", "color": "4"},
        {"id": "b2", "type": "text", "x": 400, "y": 0, "width": 260, "height": 120, "text": "Sesgo de contexto"},
        {"id": "c3", "type": "link", "x": 0, "y": 500, "width": 260, "height": 120, "url": "https://jsoncanvas.org/spec/1.0/"},
        {"id": "d4", "type": "text", "x": 400, "y": 500, "width": 260, "height": 120, "text": "[[Faustus]]"},
    ],
    "edges": [
        {"id": "e1", "fromNode": "a1", "toNode": "b2", "label": "evita"},
        {"id": "e2", "fromNode": "a1", "toNode": "c3"},
        {"id": "e3", "fromNode": "d4", "toNode": "a1", "label": "usa"},
        {"id": "e4", "fromNode": "b2", "toNode": "a1", "fromEnd": "arrow", "toEnd": "none"},
    ],
}


def test_import_creates_notes_links_tags_and_is_idempotent():
    _seed()
    report = canvas.import_canvas(OWNER, json.dumps(EXTERNAL), name="Ideas")
    assert report["created"] == 3 and report["linked_existing"] == 1
    # e4 has its arrow at the start, so it is a1 -> b2 again: not written twice
    assert report["edges_written"] == 3 and report["edges_duplicate"] == 1 and report["edges_via_index"] == 0
    pez = notes.read_note(OWNER, "Canvas/Ideas/Pez y elefante.md")
    assert pez["frontmatter"]["canvas_id"] == "a1" and pez["frontmatter"]["tags"] == ["canvas/ideas-clave"]
    targets = {link["target"] for link in pez["links"]}
    assert {"Sesgo de contexto", "jsoncanvas.org spec 1.0"} <= targets
    link_note = notes.read_note(OWNER, "Canvas/Ideas/jsoncanvas.org spec 1.0.md")
    assert link_note["frontmatter"]["url"] == "https://jsoncanvas.org/spec/1.0/"
    faustus = notes.read_note(OWNER, "Notes/Faustus.md")
    assert "%% canvas:Ideas %%" in faustus["content"] and "usa: [[Pez y elefante]]" in faustus["content"]
    assert any(t == "canvas/ideas-clave" for t in notes.read_note(OWNER, "Canvas/Ideas/Sesgo de contexto.md")["tags"])
    assert os.path.exists(os.path.join(vault.vault_root(OWNER), "Canvas", "Ideas.canvas"))

    again = canvas.import_canvas(OWNER, EXTERNAL, name="Ideas")
    assert again["created"] == 0 and again["updated"] == 0 and again["unchanged"] == 3
    assert again["written"] == []


def test_reimport_after_an_edit_updates_renames_and_reports_stale():
    canvas.import_canvas(OWNER, EXTERNAL, name="Ideas")
    edited = json.loads(json.dumps(EXTERNAL))
    edited["nodes"][2]["text"] = "Sesgo acumulado"
    edited["nodes"] = [n for n in edited["nodes"] if n["id"] != "c3"]
    edited["edges"] = [e for e in edited["edges"] if e["id"] != "e2"]
    report = canvas.import_canvas(OWNER, edited, name="Ideas")
    assert report["updated"] >= 1
    paths = {n["path"] for n in notes.tree(OWNER)["notes"]}
    assert "Canvas/Ideas/Sesgo acumulado.md" in paths and "Canvas/Ideas/Sesgo de contexto.md" not in paths
    assert report["stale"] == ["Canvas/Ideas/jsoncanvas.org spec 1.0.md"]
    pruned = canvas.import_canvas(OWNER, edited, name="Ideas", prune=True)
    assert pruned["pruned"] == ["Canvas/Ideas/jsoncanvas.org spec 1.0.md"]


def test_dry_run_writes_nothing_and_invalid_canvas_is_refused():
    _seed()
    report = canvas.import_canvas(OWNER, EXTERNAL, name="Ideas", dry_run=True)
    assert report["created"] == 3 and report["canvas_path"] is None
    assert len(notes.tree(OWNER)["notes"]) == 4
    assert "canvas:" not in notes.read_note(OWNER, "Notes/Faustus.md")["content"]
    with pytest.raises(ValueError):
        canvas.import_canvas(OWNER, {"nodes": [{"id": "x", "type": "text"}]}, name="Mal")


def test_edges_out_of_generated_notes_go_to_the_index_note():
    path = "Entities/Ada.md"
    full = os.path.join(vault.vault_root(OWNER), *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("---\nkind: entity\nsource: ent:abc\n---\nAda.\n")
    notes.reindex(OWNER)
    doc = {"nodes": [{"id": "n1", "type": "file", "x": 0, "y": 0, "width": 200, "height": 80, "file": path},
                     {"id": "n2", "type": "text", "x": 400, "y": 0, "width": 200, "height": 80, "text": "Londres"}],
           "edges": [{"id": "e", "fromNode": "n1", "toNode": "n2", "label": "vive en"}]}
    report = canvas.import_canvas(OWNER, doc, name="Gente")
    assert report["edges_via_index"] == 1 and report["index_note"]
    index = notes.read_note(OWNER, report["index_note"])
    assert "[[Ada]] → vive en → [[Londres]]" in index["content"]
    with open(full, encoding="utf-8") as fh:
        assert "canvas" not in fh.read()


def test_round_trip_text_mode_into_an_empty_brain(tmp_path):
    _seed()
    exported = canvas.export_canvas(OWNER, mode="text")
    assert exported["errors"] == []
    before = _graph_signature(OWNER)
    brain_db.use_dir(str(tmp_path / "other"))
    report = canvas.import_canvas("bob", exported["canvas"], name="Copia")
    assert report["created"] == 4 and report["refs_unresolved"] == 1
    after = _graph_signature("bob")
    assert after == before
    again = canvas.export_canvas("bob", mode="text", group_by="none")
    assert again["stats"]["nodes"] == exported["stats"]["nodes"]
    assert again["stats"]["edges"] == exported["stats"]["edges"]


def test_round_trip_file_mode_into_the_same_brain_changes_nothing():
    _seed()
    before = _graph_signature(OWNER)
    exported = canvas.export_canvas(OWNER)
    report = canvas.import_canvas(OWNER, exported["canvas"], name="Mismo")
    assert report["created"] == 0 and report["linked_existing"] == 4
    assert report["edges_written"] == 0 and report["edges_duplicate"] == 5
    assert _graph_signature(OWNER) == before


def test_remove_import_trashes_notes_and_strips_blocks():
    _seed()
    canvas.import_canvas(OWNER, EXTERNAL, name="Ideas")
    out = canvas.remove_import(OWNER, "Ideas")
    assert len(out["removed"]) == 3 and out["cleaned"] == ["Notes/Faustus.md"] and out["canvas_file_removed"]
    assert "canvas:Ideas" not in notes.read_note(OWNER, "Notes/Faustus.md")["content"]
    assert len(notes.list_trash(OWNER)) == 3
    canvas.import_canvas(OWNER, EXTERNAL, name="Otra")
    purged = canvas.remove_import(OWNER, "Otra", purge=True)
    assert len(purged["removed"]) == 3 and len(notes.list_trash(OWNER)) == 3


def test_entities_scope_exports_relations_with_labels():
    from src.brain import entities
    mara = entities.upsert_entity(OWNER, "Mara", type="person")["id"]
    quito = entities.upsert_entity(OWNER, "Quito", type="place")["id"]
    entities.add_relation(OWNER, mara, "lives_in", dst_id=quito, confidence=0.9)
    out = canvas.export_canvas(OWNER, scope="entities")
    assert out["errors"] == [] and out["stats"]["nodes"] >= 2
    assert "lives_in" in [e.get("label") for e in out["canvas"]["edges"]]
    texts = {n.get("text") for n in out["canvas"]["nodes"] if n["type"] == "text"}
    assert {"**Mara**\n\n_person_", "**Quito**\n\n_place_"} <= texts


# ── surfaces: agent tools, MCP, REST ─────────────────────────────────────

def test_agent_tools_export_save_and_import_from_a_path(tmp_path):
    import asyncio
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert {"brain_canvas_export", "brain_canvas_import"} <= names
    _seed()
    ctx = {"owner": OWNER}
    out = asyncio.run(TOOL_HANDLERS["brain_canvas_export"](json.dumps({"center": "Faustus", "depth": 1, "name": "Mapa"}), ctx))
    data = json.loads(out["output"])
    assert out["exit_code"] == 0 and data["path"] == "Canvas/Mapa.canvas" and data["errors"] == []
    target = tmp_path / "ideas.canvas"
    target.write_text(json.dumps(EXTERNAL), encoding="utf-8")
    out = asyncio.run(TOOL_HANDLERS["brain_canvas_import"](json.dumps({"path": str(target)}), ctx))
    report = json.loads(out["output"])
    assert report["name"] == "ideas" and report["created"] == 3
    out = asyncio.run(TOOL_HANDLERS["brain_canvas_import"](json.dumps({"undo": True, "name": "ideas", "purge": True}), ctx))
    assert len(json.loads(out["output"])["removed"]) == 3
    bad = asyncio.run(TOOL_HANDLERS["brain_canvas_import"](json.dumps({"canvas": {"nodes": [{"id": 1}]}}), ctx))
    assert bad["exit_code"] == 1 and "not a valid JSON Canvas" in bad["error"]


def test_rest_routes_round_trip(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.brain_routes as br

    monkeypatch.setattr(br, "_owner", lambda request: OWNER)
    app = FastAPI()
    router = br.setup_brain_routes()
    app.dependency_overrides[br.require_user] = lambda: OWNER
    app.include_router(router)
    client = TestClient(app)
    _seed()
    resp = client.post("/api/brain/canvas/export", json={"scope": "notes", "save": True, "name": "Rest"})
    assert resp.status_code == 200 and resp.json()["path"] == "Canvas/Rest.canvas"
    resp = client.get("/api/brain/canvas/export.canvas", params={"center": "Ollama", "name": "Ollama"})
    assert resp.status_code == 200 and "Ollama.canvas" in resp.headers["content-disposition"]
    assert canvas.validate(resp.json()) == []
    resp = client.post("/api/brain/canvas/validate", json={"canvas": {"nodes": [{"id": "a"}]}})
    assert resp.json()["ok"] is False
    resp = client.post("/api/brain/canvas/import", json={"canvas": json.dumps(EXTERNAL), "name": "Rest"})
    assert resp.status_code == 200 and resp.json()["created"] == 3
    resp = client.post("/api/brain/canvas/import", json={"canvas": {"nodes": "x"}, "name": "Mal"})
    assert resp.status_code == 400
    resp = client.delete("/api/brain/canvas/import", params={"name": "Rest", "purge": "true"})
    assert resp.status_code == 200 and len(resp.json()["removed"]) == 3
