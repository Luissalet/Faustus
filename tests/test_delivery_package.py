"""Reproducible delivery package (Hangar E33): a package is ready only when every
editable and export reopens with its format reader, both from the source and
from the extracted zip; data without authorization stays out; gaps are named.
Exercised on the three kinds of delivery the proposal names: software, a 3D
product sheet and a presentation."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from src import constants as constants_mod
from src import delivery_package as dp


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(dp, "_confine", lambda p: Path(str(p)).resolve())
    yield


def _stl(path: Path, tris: int = 2) -> Path:
    body = bytearray(b"\0" * 80) + struct.pack("<I", tris)
    for i in range(tris):
        body += struct.pack("<3f", 0, 0, 1)
        body += struct.pack("<9f", 0, 0, 0, 10, 0, 0, 0, 20 + i, 5)
        body += b"\0\0"
    path.write_bytes(bytes(body))
    return path


def _glb(path: Path) -> Path:
    doc = {"asset": {"version": "2.0"}, "meshes": [{"primitives": []}], "nodes": [{"mesh": 0}],
           "buffers": [{"byteLength": 4}]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    binc = b"\0\0\0\0"
    total = 12 + 8 + len(js) + 8 + len(binc)
    path.write_bytes(b"glTF" + struct.pack("<II", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
                     + struct.pack("<II", len(binc), 0x004E4942) + binc)
    return path


def _3mf(path: Path) -> Path:
    model = ('<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             '<resources><object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/>'
             '<vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles>'
             '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", model)
    return path


def _pptx(path: Path) -> Path:
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Propuesta"
    slide.placeholders[1].text = "Alcance y plazos"
    prs.save(str(path))
    return path


def _ok_args(name, items, **extra):
    return {"action": "build", "name": name, "items": items, "instructions": "Abrir el fichero editable.",
            "limitations": ["Datos de ejemplo inventados."], **extra}


def test_3d_sheet_is_ready_and_reopens_from_the_zip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pieza.blend").write_bytes(b"BLENDER-v402" + b"\0" * 64)
    items = [{"path": str(src / "pieza.blend"), "role": "editable"},
             {"path": str(_stl(src / "pieza.stl")), "role": "export"},
             {"path": str(_3mf(src / "pieza.3mf")), "role": "export"},
             {"path": str(_glb(src / "pieza.glb")), "role": "export"}]
    m = dp.run("alice", _ok_args("Ficha 3D", items))
    assert m.get("ready") is True, m.get("gaps")
    by = {it["name"]: it for it in m["items"]}
    assert by["pieza.stl"]["verification"]["facts"]["triangles"] == 2
    assert by["pieza.3mf"]["verification"]["facts"]["unit"] == "millimeter"
    assert by["pieza.glb"]["verification"]["facts"]["meshes"] == 1
    assert by["pieza.blend"]["verification"]["level"] == "container signature only"
    assert m["reimport"]["ok"] and all(r["same_hash"] for r in m["reimport"]["items"] if "same_hash" in r)
    with zipfile.ZipFile(m["zip"]["path"]) as z:
        names = z.namelist()
    assert "Ficha-3D/ENTREGA.md" in names and "Ficha-3D/exports/pieza.glb" in names
    assert "Lista para entregar" in Path(m["zip"]["path"]).parent.joinpath("Ficha-3D", "ENTREGA.md").read_text("utf-8")


def test_presentation_with_a_broken_export_is_not_ready(tmp_path):
    deck = _pptx(tmp_path / "propuesta.pptx")
    broken = tmp_path / "propuesta.pdf"
    broken.write_bytes(b"%PDF-1.7 truncated")
    m = dp.run("alice", _ok_args("Propuesta", [{"path": str(deck), "role": "editable"},
                                               {"path": str(broken), "role": "export"}]))
    assert m["ready"] is False
    by = {it["name"]: it for it in m["items"]}
    assert by["propuesta.pptx"]["verification"]["status"] == "verified"
    assert by["propuesta.pdf"]["verification"]["status"] == "failed"
    assert any("propuesta.pdf does not open" in g for g in m["gaps"])
    md = (Path(m["zip"]["path"]).parent / "Propuesta" / "ENTREGA.md").read_text("utf-8")
    assert "NO lista para entregar" in md and "failed" in md


def test_software_delivery_uses_the_verified_handoff(tmp_path):
    repo = tmp_path / "app"
    (repo / ".handoff").mkdir(parents=True)
    (repo / "main.py").write_text("print('hola')\n", encoding="utf-8")
    (repo / ".handoff" / "HANDOFF.md").write_text("# Handoff\n\npython main.py\n", encoding="utf-8")
    (repo / ".handoff" / "handoff-report.json").write_text(json.dumps({
        "counts": {"verified": 2, "failed": 0, "not run": 1},
        "steps": [{"title": "run", "verdict": "verified"}], "revision": "abc"}), encoding="utf-8")
    m = dp.run("alice", _ok_args("App", [{"path": str(repo / "main.py"), "role": "editable"}],
                                 software_root=str(repo)))
    assert m["ready"] is True, m["gaps"]
    assert m["software"]["counts"]["verified"] == 2
    assert {it["package_path"] for it in m["items"]} >= {"instructions/HANDOFF.md", "instructions/handoff-report.json"}


def test_software_without_a_handoff_is_a_named_gap(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "main.py").write_text("def broken(:\n", encoding="utf-8")
    m = dp.run("alice", _ok_args("App", [{"path": str(repo / "main.py"), "role": "editable"}],
                                 software_root=str(repo)))
    assert m["ready"] is False
    assert any("handoff not verified" in g for g in m["gaps"])
    assert m["items"][0]["verification"]["status"] == "failed"
    assert "does not compile" in m["items"][0]["verification"]["reason"]


def test_data_without_authorization_stays_out_and_limitations_are_required(tmp_path):
    data = tmp_path / "clientes.csv"
    data.write_text("id,nombre\n1,Ana\n", encoding="utf-8")
    shared = tmp_path / "tarifas.csv"
    shared.write_text("material,eur\nPLA,20\n", encoding="utf-8")
    model = _stl(tmp_path / "p.stl")
    text_items = (f"editable: {model}\nexport: {model}\ndata: {data}\ndata: {shared} (authorized)\n")
    m = dp.run("alice", {"action": "build", "name": "Datos", "items": text_items, "instructions": "x"})
    assert [x["path"] for x in m["excluded"]] == [str(data.resolve())]
    assert any(it["name"] == "tarifas.csv" and it["role"] == "data" for it in m["items"])
    assert m["ready"] is False and any("no limitations" in g for g in m["gaps"])


def test_unknown_export_format_keeps_it_from_ready(tmp_path):
    odd = tmp_path / "pieza.xyz"
    odd.write_bytes(b"whatever")
    m = dp.run("alice", _ok_args("Raro", [{"path": str(_stl(tmp_path / "a.stl")), "role": "editable"},
                                          {"path": str(odd), "role": "export"}]))
    assert m["ready"] is False
    assert m["items"][1]["verification"]["status"] == "not_checked"


def test_list_show_and_reverify_detect_a_changed_zip(tmp_path):
    m = dp.run("alice", _ok_args("Uno", [{"path": str(_stl(tmp_path / "a.stl")), "role": "editable"}]))
    rows = dp.run("alice", {"action": "list"})["packages"]
    assert rows[0]["id"] == m["id"] and rows[0]["ready"] is True
    assert dp.run("bob", {"action": "list"})["packages"] == []
    assert dp.run("alice", {"action": "show", "id": m["id"]})["name"] == "Uno"
    assert dp.run("alice", {"action": "reverify", "id": m["id"]})["reverify"]["ok"] is True
    zp = Path(m["zip"]["path"])
    with zipfile.ZipFile(zp, "a") as z:
        z.writestr("Uno/editables/a.stl", b"tampered")
    assert dp.run("alice", {"action": "reverify", "id": m["id"]})["reverify"]["ok"] is False
    assert dp.run("alice", {"action": "show", "id": "../x"})["error_class"] == "delivery.not_found"


def test_bad_arguments_are_errors_not_exceptions(tmp_path):
    assert dp.run("alice", {"action": "build"})["error_class"] == "delivery.bad_args"
    assert dp.run("alice", {"action": "nope"})["error_class"] == "delivery.bad_args"
    bad = dp.run("alice", {"action": "build", "name": "x", "items": [{"path": str(tmp_path), "role": "boss"}]})
    assert bad["error_class"] == "delivery.bad_role"
    assert ".glb" in dp.run("alice", {"action": "readers"})["readers"]


def test_package_documents_carry_no_local_paths(tmp_path):
    work = tmp_path / "Mis datos privados"
    work.mkdir()
    (work / "nota.txt").write_text("hola\n", encoding="utf-8")
    (work / "datos.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    r = dp.run("u", {"name": "T", "instructions": "x", "limitations": ["y"], "items": [
        {"path": str(work / "nota.txt"), "role": "editable"},
        {"path": str(work / "datos.csv"), "role": "data"},
        {"path": str(work / "falta.stl"), "role": "export"}]}, confine=False)
    assert "Mis datos privados" in r["items"][0]["source_path"]          # the owner still sees where it came from
    with zipfile.ZipFile(r["zip"]["path"]) as z:
        texts = [z.read(n).decode("utf-8") for n in z.namelist() if n.endswith((".md", ".json"))]
    assert texts
    for text in texts:
        assert "Mis datos privados" not in text and tmp_path.name not in text
    assert any("datos.csv" in t for t in texts)                          # the exclusion is still listed, by name
