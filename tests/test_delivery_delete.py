"""Deleting Delivery records (packages, scope comparisons, review desks, commissions, meetings):
only Faustus' own records and copies go; the user's source files never do."""
from __future__ import annotations

import json
import os

import pytest

from src import commission_office as co
from src import constants as constants_mod
from src import delivery_package as dp
from src import meetings
from src import review_desk as rd
from src import scope_compare as sc


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(meetings, "MEETINGS_DIR", str(tmp_path / "meetings"))
    monkeypatch.setattr(meetings, "JOBS_FILE", str(tmp_path / "meetings" / "_jobs.json"))
    yield


def _file(tmp_path, name="nota.txt", text="hola\n"):
    f = tmp_path / "src" / name
    f.parent.mkdir(exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


def _package(tmp_path, name="Entrega"):
    f = _file(tmp_path)
    out = tmp_path / "copia"
    r = dp.run("u", {"name": name, "instructions": "x", "limitations": ["y"], "out_dir": str(out),
                     "items": [{"path": str(f), "role": "editable"}]}, confine=False)
    assert r["ready"], r
    return r, f, out


def test_delete_package_removes_faustus_copy_only(tmp_path):
    pkg, src, out = _package(tmp_path)
    home = os.path.dirname(pkg["zip"]["path"])
    res = dp.run("u", {"action": "delete", "id": pkg["id"]})
    assert res["deleted"] == pkg["id"] and not os.path.exists(home)
    assert src.is_file()                                            # the user's source
    assert (out / os.path.basename(pkg["zip"]["path"])).is_file()   # a zip copied where the user asked
    assert res["kept"]["zip_copy_outside_faustus"]
    assert dp.run("u", {"action": "show", "id": pkg["id"]})["error_class"] == "delivery.not_found"
    assert dp.run("u", {"action": "delete", "id": pkg["id"]})["error_class"] == "delivery.not_found"
    assert dp.run("u", {"action": "delete", "id": "../../x"})["error_class"] == "delivery.not_found"


def test_delete_scope_comparison():
    c = sc.run("u", {"action": "compare", "agreement": "Entregar informe en PDF.", "new_request": "Entregar informe en DOCX."})
    res = sc.run("u", {"action": "delete", "id": c["id"]})
    assert res["deleted"] == c["id"]
    assert sc.run("u", {"action": "list"})["comparisons"] == []
    assert sc.run("u", {"action": "delete", "id": c["id"]})["error_class"] == "scope.not_found"


def test_delete_review_desk_keeps_source(tmp_path):
    f = _file(tmp_path, "informe.txt")
    d = rd.run("u", {"action": "create", "title": "Informe", "path": str(f)}, confine=False)
    snap = d["versions"][0]["snapshot"]
    assert os.path.isfile(snap)
    res = rd.run("u", {"action": "delete", "id": d["id"]})
    assert res["deleted"] == d["id"] and res["removed"]["versions"] == 1
    assert not os.path.exists(snap) and f.is_file()
    assert rd.run("u", {"action": "list"})["desks"] == []
    assert rd.run("u", {"action": "delete", "id": d["id"]})["error_class"] == "desk.not_found"


def _commission(tmp_path):
    rec = co.run("u", {"action": "create", "title": "Caja", "client": "ACME", "request": "Entregar informe en PDF."})
    cid = rec["id"]
    co.run("u", {"action": "add_scope", "id": cid, "kind": "agreement", "text": "Entregar informe en PDF."})
    co.run("u", {"action": "add_scope", "id": cid, "kind": "change", "text": "Entregar informe en DOCX."})
    co.run("u", {"action": "add_milestone", "id": cid, "title": "M uno", "deliverables": ["Informe"]})
    return cid


def test_delete_commission_keeps_linked_items_by_default(tmp_path):
    cid = _commission(tmp_path)
    pkg, src, _ = _package(tmp_path)
    co.run("u", {"action": "link_package", "id": cid, "milestone": "M1", "package_id": pkg["id"]})
    desk = rd.run("u", {"action": "create", "title": "Informe", "path": str(src), "commission": cid}, confine=False)
    co.run("u", {"action": "add_file", "id": cid, "path": str(src)})
    co.run("u", {"action": "proposal", "id": cid})
    assert sc.run("u", {"action": "list"})["comparisons"]
    res = co.run("u", {"action": "delete", "id": cid})
    assert res["deleted"] == cid
    assert res["removed"]["proposals"] == 1 and res["removed"]["scope_comparisons"] and not res["removed"]["packages"]
    assert [p["id"] for p in res["kept"]["packages"]] == [pkg["id"]] and [d["id"] for d in res["kept"]["review_desks"]] == [desk["id"]]
    assert co.run("u", {"action": "list"})["commissions"] == []
    assert sc.run("u", {"action": "list"})["comparisons"] == []
    assert dp.run("u", {"action": "show", "id": pkg["id"]})["id"] == pkg["id"]
    assert rd.run("u", {"action": "show", "id": desk["id"]})["id"] == desk["id"]
    assert src.is_file()
    assert not (co._dir("u") / cid).exists()


def test_delete_commission_with_linked_removes_packages_and_desks_but_not_shared_ones(tmp_path):
    a = _commission(tmp_path)
    b = co.run("u", {"action": "create", "title": "Otra", "client": "ACME"})["id"]
    co.run("u", {"action": "add_milestone", "id": b, "title": "M uno", "deliverables": ["Informe"]})
    own, src, _ = _package(tmp_path, "Propio")
    shared, _, _ = _package(tmp_path, "Compartido")
    co.run("u", {"action": "link_package", "id": a, "milestone": "M1", "package_id": own["id"]})
    co.run("u", {"action": "link_package", "id": a, "milestone": "M1", "package_id": shared["id"], "deliverable": "Otro"})
    co.run("u", {"action": "link_package", "id": b, "milestone": "M1", "package_id": shared["id"]})
    desk = rd.run("u", {"action": "create", "title": "Informe", "path": str(src), "commission": a}, confine=False)
    res = co.run("u", {"action": "delete", "id": a, "with_linked": True})
    assert res["removed"]["packages"] == [own["id"]] and res["removed"]["review_desks"] == [desk["id"]]
    assert [p["id"] for p in res["kept"]["packages"]] == [shared["id"]]
    assert dp.run("u", {"action": "show", "id": own["id"]})["error_class"] == "delivery.not_found"
    assert dp.run("u", {"action": "show", "id": shared["id"]})["id"] == shared["id"]
    assert src.is_file()
    # the other commission now reports where the package lives, and deleting the package says who linked it
    res = dp.run("u", {"action": "delete", "id": shared["id"]})
    assert [c["id"] for c in res["was_linked_in"]] == [b]
    assert co.run("u", {"action": "status", "id": b})["status"]["deliverables_without_ready_package"].count("Compartido") == 1


def test_delete_meeting_removes_notes_only(tmp_path):
    os.makedirs(meetings.MEETINGS_DIR, exist_ok=True)
    mid = "2026-11-04-qa-reunion"
    with open(os.path.join(meetings.MEETINGS_DIR, mid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": mid, "title": "QA", "owner": "u"}, fh)
    with open(os.path.join(meetings.MEETINGS_DIR, mid + ".md"), "w", encoding="utf-8") as fh:
        fh.write("[00:00:05] Ana: Hay que entregar en PDF.\n")
    audio = tmp_path / "meetings" / "grabacion.wav"
    audio.write_bytes(b"RIFF")
    cid = _commission(tmp_path)
    co.run("u", {"action": "import_meeting", "id": cid, "meeting_id": mid})
    res = co.run("u", {"action": "delete_meeting", "meeting_id": mid})
    assert res["removed"] == [mid + ".json", mid + ".md"] and [c["id"] for c in res["kept"]["copies_in_commissions"]] == [cid]
    assert audio.is_file()
    assert co.run("u", {"action": "list_meetings"})["meetings"] == []
    assert co.run("u", {"action": "show", "id": cid})["meetings"][0]["items"]     # the commission keeps its copy
    assert co.run("u", {"action": "delete_meeting", "meeting_id": mid})["error_class"] == "office.not_found"
    assert co.run("u", {"action": "delete_meeting", "meeting_id": "../x"})["error_class"] == "office.not_found"
