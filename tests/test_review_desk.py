"""Review desk (Hangar E32) and shared anchors (E22): versions are snapshots,
comments stay on the version they were made on, older feedback is recognised,
one unambiguous version is delivered."""
from __future__ import annotations

import pytest

from src import anchors
from src import constants as constants_mod
from src import review_desk as rd


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def test_anchor_wording_and_refs():
    assert anchors.parse("en la página 4 el logo")["label"] == "p4"
    assert anchors.parse("p3@120,80,200,40")["rect"] == [120, 80, 200, 40]
    assert anchors.parse("min 1:10-1:25 sobra")["label"] == "t=00:01:10-00:01:25"
    assert anchors.parse("Presupuesto!C12 está mal")["label"] == "Presupuesto!C12"
    assert anchors.parse("la pieza Tapa roza")["label"] == "obj:Tapa"
    assert anchors.parse("líneas 10-20")["label"] == "L10-20"
    assert anchors.parse("función compute_total falla")["label"] == "sym:compute_total"
    assert anchors.parse("tornillos M3 en la tapa") is None
    r = anchors.parse_ref(anchors.ref("caja-final", 2, anchors.parse("p3@1,2,3,4")))
    assert r["desk"] == "caja-final" and r["version"] == 2 and r["anchor"]["label"] == "p3@1,2,3,4"


def test_versions_comments_and_delivery(tmp_path):
    f = tmp_path / "final.md"
    f.write_text("Título\nLogo arriba\nPie de página\n", encoding="utf-8")
    desk = rd.run("alice", {"action": "create", "title": "Informe", "path": str(f)}, confine=False)
    did = desk["id"]
    assert desk["versions"][0]["n"] == 1 and desk["versions"][0]["reopen"]["status"] == "verified"
    c1 = rd.run("alice", {"action": "comment", "id": did, "text": "página 2: el logo está torcido"})["comment"]
    assert c1["version"] == 1 and c1["anchor"]["label"] == "p2" and c1["ref"] == f"review:{did}@v1#p2"
    f.write_text("Título\nLogo centrado\nPie de página\nAnexo\n", encoding="utf-8")
    rec = rd.run("alice", {"action": "add_version", "id": did, "path": str(f)}, confine=False)
    assert rec["status"]["latest"] == 2 and rec["status"]["open_from_older"] == ["C1"]
    again = rd.run("alice", {"action": "add_version", "id": did, "path": str(f)}, confine=False)
    assert again["error_class"] == "desk.same_content"
    rec = rd.run("alice", {"action": "import_feedback", "id": did, "text": "Sobre la v1:\n- p3 cambiar el color\n\nEn la v2 falta el índice"})
    c2, c3 = rec["imported"]
    assert c2["version"] == 1 and c2["about_older"] and c2["version_from"] == "named in the feedback"
    assert c3["version"] == 2 and not c3["about_older"] and c3["version_from"] == "named in the text"
    cmp = rd.run("alice", {"action": "compare", "id": did})["comparison"]
    assert cmp["a"] == 1 and cmp["b"] == 2 and cmp["text"]["added"] == 2 and cmp["text"]["removed"] == 1
    assert [c["id"] for c in cmp["comments_on_a"]] == ["C1", "C2"]
    assert rd.run("alice", {"action": "deliver", "id": did})["error_class"] == "desk.open_comments"
    for cid in ("C1", "C2"):
        assert rd.run("alice", {"action": "set_status", "id": did, "comment": cid, "status": "fixed"})["comment"]["fixed_in"] == 2
    rd.run("alice", {"action": "set_status", "id": did, "comment": "C3", "status": "wontfix", "note": "no aplica"})
    rec = rd.run("alice", {"action": "deliver", "id": did, "version": 2})
    st = rec["status"]
    assert st["deliverable"].startswith("v2 final.md") and st["delivered_check"]["snapshot_matches"] is True
    assert st["delivered_check"]["source_still_matches"] is True
    f.write_text("cambiado después de entregar\n", encoding="utf-8")
    st = rd.run("alice", {"action": "status", "id": did})["status"]
    assert st["delivered_check"]["source_still_matches"] is False and st["delivered_check"]["snapshot_matches"] is True
    assert rd.run("bob", {"action": "show", "id": did})["error_class"] == "desk.not_found"
    assert rd.run("alice", {"action": "list"})["desks"][0]["delivered"] == 2


def test_review_desk_surfaces(tmp_path, monkeypatch):
    import asyncio
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.delivery_routes as dr
    from src import tool_index, tool_schemas
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index_examples import EXAMPLES
    monkeypatch.setattr(dr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(dr.setup_delivery_routes())
    client = TestClient(app)
    f = tmp_path / "plano.txt"
    f.write_text("cota 10\n", encoding="utf-8")
    desk = client.post("/api/review-desks", json={"title": "Plano", "path": str(f)}).json()
    did = desk["id"]
    f.write_text("cota 12\n", encoding="utf-8")
    assert client.post(f"/api/review-desks/{did}/add_version", json={"path": str(f)}).json()["status"]["latest"] == 2
    c = client.post(f"/api/review-desks/{did}/comment", json={"text": "L1 la cota está mal", "version": 1}).json()["comment"]
    assert c["about_older"] and c["anchor"]["label"] == "L1"
    assert client.get(f"/api/review-desks/{did}/compare").json()["comparison"]["text"]["added"] == 1
    assert client.get(f"/api/review-desks/{did}/versions/1").text.splitlines() == ["cota 10"]
    assert client.post(f"/api/review-desks/{did}/deliver", json={}).status_code == 400
    assert client.post(f"/api/review-desks/{did}/nope", json={}).status_code == 400
    assert client.get("/api/review-desks/zz-unknown").status_code == 404
    assert client.get("/api/review-desks").json()["desks"][0]["id"] == did
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert "review_desk" in TOOL_HANDLERS and "review_desk" in TOOL_TAGS and "review_desk" in schemas
    assert "review_desk" in tool_index.BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES["review_desk"] and TOOL_CAPABILITIES["review_desk"].effects
    out = asyncio.run(TOOL_HANDLERS["review_desk"](json.dumps({"action": "show", "id": did}), {"owner": "alice"}))
    assert out["exit_code"] == 0 and "v2 plano.txt" in out["output"]
    import mcp_servers.delivery_server as srv
    monkeypatch.setenv("ODYSSEUS_MCP_DELIVERY_OWNER", "alice")
    res = json.loads(asyncio.run(srv.call_tool("review_desk", {"action": "list"}))[0].text)
    assert res["desks"][0]["id"] == did


def test_ordinary_words_are_not_anchors():
    for text in ("The first part needs work", "This class of problem is wrong", "la pieza grande se ve mal",
                 "Please fix function names"):
        assert anchors.parse(text) is None, text
    assert anchors.parse("la pieza Tapa roza")["label"] == "obj:Tapa"
    assert anchors.parse("revisar la clase `Informe` entera")["label"] == "sym:Informe"
    assert anchors.parse("the part: lid is loose")["label"] == "obj:lid"
