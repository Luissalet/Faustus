"""Hangar E22: a version of a deliverable opened exactly at a shared anchor, from the review desk (REST, tool) and by reference."""
from __future__ import annotations

import json
import struct

import pytest

from src import anchor_view as av
from src import anchors
from src import constants as constants_mod
from src import review_desk as rd


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def _pdf(path, pages=2):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 100), f"Pagina {i + 1}: tolerancia 0,2 mm")
    doc.save(str(path))
    doc.close()


def test_page_with_rectangle_text_and_image(tmp_path):
    p = tmp_path / "ficha.pdf"
    _pdf(p)
    v = av.view(p, anchors.parse("p2@60,80,300,40"), image=True)
    assert v["found"] and v["pages"] == 2 and "Pagina 2" in v["text_in_rect"] and v["image"].startswith("data:image/png;base64,")
    past = av.view(p, anchors.parse("p9"))
    assert past["found"] is False and past["pages"] == 2


def test_cells_lines_symbols_objects_and_media(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Presupuesto"
    ws["C12"] = 617.83
    ws["B12"] = "Total"
    x = tmp_path / "p.xlsx"
    wb.save(str(x))
    cell = av.view(x, anchors.parse("Presupuesto!C12"))
    assert cell["found"] and cell["value"] == "617.83" and any(c["hit"] and c["ref"] == "C12" for r in cell["grid"] for c in r["cells"])
    assert av.view(x, anchors.parse("Otra!C12"))["found"] is False
    code = tmp_path / "calc.py"
    code.write_text("import math\n\n\ndef compute_total(rows):\n    s = 0\n    for r in rows:\n        s += r\n    return s\n\n\nx = 1\n", encoding="utf-8")
    sym = av.view(code, anchors.parse("sym:compute_total"))
    assert sym["found"] and sym["declared_at"] == 4 and [ln["n"] for ln in sym["lines"] if ln["hit"]][:1] == [4]
    line = av.view(code, anchors.parse("L7"))
    assert line["found"] and [ln["n"] for ln in line["lines"]] == list(range(4, 11))
    obj = tmp_path / "caja.obj"
    obj.write_text("o Carcasa\nv 0 0 0\no Tapa\nv 1 1 1\n", encoding="utf-8")
    hit = av.view(obj, anchors.parse("obj:Tapa"))
    assert hit["found"] and hit["objects"] == ["Carcasa", "Tapa"]
    gltf = {"asset": {"version": "2.0"}, "nodes": [{"name": "Tapa"}], "meshes": [{"name": "TapaMesh"}]}
    raw = json.dumps(gltf).encode()
    raw += b" " * (-len(raw) % 4)
    glb = tmp_path / "caja.glb"
    glb.write_bytes(struct.pack("<III", 0x46546C67, 2, 20 + len(raw)) + struct.pack("<II", len(raw), 0x4E4F534A) + raw)
    assert av.object_names(glb) == ["Tapa", "TapaMesh"]
    media = tmp_path / "demo.mp4"
    media.write_bytes(b"\x00")
    t = av.view(media, anchors.parse("t=00:01:10-00:01:25"))
    assert t["found"] and t["fragment"] == "#t=70,85" and t["media"] == "video"
    wrong = av.view(code, anchors.parse("p3"))
    assert wrong["found"] is False and "PDF" in wrong["reason"]


def test_the_desk_opens_a_comment_where_it_was_made_even_after_a_new_version(tmp_path):
    p = tmp_path / "ficha.pdf"
    _pdf(p)
    desk = rd.run("alice", {"action": "create", "title": "Ficha", "path": str(p)}, confine=False)
    c = rd.run("alice", {"action": "comment", "id": desk["id"], "text": "Cambiar la tolerancia", "anchor": "p2@60,80,300,40"})["comment"]
    _pdf(p, pages=1)                                        # the next version drops page 2
    rd.run("alice", {"action": "add_version", "id": desk["id"], "path": str(p)}, confine=False)
    v = rd.run("alice", {"action": "view", "id": desk["id"], "comment": c["id"]})["view"]
    assert v["version"] == 1 and v["latest"] == 2 and v["found"] and v["from_snapshot"] and v["ref"] == c["ref"]
    same = rd.run("alice", {"action": "view", "ref": c["ref"]})
    assert same["id"] == desk["id"] and same["view"]["page"] == 2
    gone = rd.run("alice", {"action": "view", "id": desk["id"], "version": 2, "anchor": "p2"})["view"]
    assert gone["found"] is False and gone["pages"] == 1          # the place no longer exists in v2, and it says so
    assert "older than v2" in rd.render(same)
    assert rd.run("alice", {"action": "view", "ref": "review:otra-000@v1#p1"})["error_class"] == "desk.not_found"


def test_a_spreadsheet_is_closed_after_opening_it_at_a_cell(tmp_path):
    # an open read-only workbook keeps the file locked on Windows, which blocks replacing or deleting the version later
    from openpyxl import Workbook
    wb = Workbook()
    wb.active["B2"] = "hola"
    f = tmp_path / "hoja.xlsx"
    wb.save(f)
    out = av.view(f, {"kind": "cell", "cell": "B2"})
    assert out["found"] is True and out["value"] == "hola"
    f.unlink()                      # PermissionError while the workbook handle is still open
    assert not f.exists()


def test_a_backwards_line_range_marks_the_same_lines_as_the_forward_one(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"linea {i}" for i in range(1, 11)), encoding="utf-8")
    fwd = av.view(f, {"kind": "line", "start": 3, "end": 5})
    back = av.view(f, {"kind": "line", "start": 5, "end": 3})
    assert [x["n"] for x in back["lines"] if x["hit"]] == [3, 4, 5] and back["lines"] == fwd["lines"]


def test_a_cell_range_is_parsed_in_any_order_and_marks_every_cell_of_the_range(tmp_path):
    from openpyxl import Workbook
    a = anchors.parse("Datos!B3:A1")
    assert a["cell"] == "A1" and a["to"] == "B3" and a["label"] == "Datos!A1:B3"
    assert anchors.parse_ref("review:x-abc@v2#Datos!A1:B3")["anchor"]["to"] == "B3"
    assert anchors.parse("Datos!A1").get("to") is None and anchors.parse("Datos!A1:A1").get("to") is None
    wb = Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws["A1"], ws["B3"] = "uno", "dos"
    f = tmp_path / "r.xlsx"
    wb.save(f)
    out = av.view(f, a)
    hits = {c["ref"] for row in out["grid"] for c in row["cells"] if c["hit"]}
    assert out["found"] and out["to"] == "B3" and out["cells_in_range"] == 6 and hits == {"A1", "B1", "A2", "B2", "A3", "B3"}
    empty = av.view(f, anchors.parse("Datos!D5:E6"))
    assert empty["found"] is False
    csv = tmp_path / "r.csv"
    csv.write_text("a,b\n,c\n", encoding="utf-8")
    assert av.view(csv, {"kind": "cell", "sheet": "", "cell": "A1", "to": "B2"})["found"] is True


def test_an_object_anchor_without_spaces_finds_an_object_named_with_spaces(tmp_path):
    f = tmp_path / "ruedas.obj"
    f.write_text("o Rueda dcha\nv 0 0 0\no Base\n", encoding="utf-8")
    out = av.view(f, anchors.parse("obj:Rueda_dcha"))
    assert out["found"] is True and out["object"] == "Rueda dcha"
