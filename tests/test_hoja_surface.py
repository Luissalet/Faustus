"""Hoja as a work surface: the routes, the four agent tools, what the window is told while the agent writes, the
context block and every registration point."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import hoja, surfaces, tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS, sheet_tools

NAMES = ["sheet_create", "sheet_read", "sheet_write", "sheet_query"]


@pytest.fixture(autouse=True)
def clean(tmp_path):
    hoja.set_root_for_tests(str(tmp_path / "hoja"))
    surfaces.reset_for_tests()
    yield
    hoja.set_root_for_tests(None)
    surfaces.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


def _client(monkeypatch):
    from routes import sheet_routes as sr
    monkeypatch.setattr(sr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(sr.setup_sheet_routes())
    return TestClient(app)


def _tool(name, args, session="s1"):
    return run(TOOL_HANDLERS[name](args, {"session_id": session}))


def test_routes(monkeypatch):
    c = _client(monkeypatch)
    sid = c.post("/api/hoja", json={"title": "R", "session": "s9", "csv": "item,qty\na,2\nb,3\n"}).json()["id"]
    assert [s["id"] for s in c.get("/api/hoja", params={"session": "s9"}).json()["sheets"]] == [sid]
    got = c.get(f"/api/hoja/{sid}").json()
    assert got["meta"]["title"] == "R" and got["tabs"][0]["used"] == [3, 2]
    w = c.get(f"/api/hoja/{sid}/window", params={"r0": 1, "c0": 1, "r1": 10, "c1": 5}).json()
    assert {(x[0], x[1]): x[3] for x in w["cells"]}[(2, 2)] == "2"
    e = c.post(f"/api/hoja/{sid}/edit", json={"client": "w1", "view": {"tab": got["tabs"][0]["id"]}, "ops": [{"op": "set", "at": "B4", "values": [["=SUM(B2:B3)"]]}]}).json()
    assert e["rev"] == 2 and e["tabs"][got["tabs"][0]["id"]]["cells"]
    assert {(x[0], x[1]): x[3] for x in c.get(f"/api/hoja/{sid}/window", params={"r0": 4, "c0": 2, "r1": 4, "c1": 2}).json()["cells"]}[(4, 2)] == "5"
    bad = c.post(f"/api/hoja/{sid}/edit", json={"ops": [{"op": "set", "at": "A1", "values": [["=SUM(("]]}]})
    assert bad.status_code == 200 and bad.json()["error"]["code"] == "bad_formula"
    ops = c.post(f"/api/hoja/{sid}/ops", json={"session": "s9", "ops": [{"op": "format", "range": "A1:B1", "bold": True}]}).json()
    assert ops["results"] and ops["rev"] == 3
    assert c.post(f"/api/hoja/{sid}/undo", json={}).json()["ok"] is True
    assert c.post(f"/api/hoja/{sid}/redo", json={}).json()["ok"] is True
    assert c.post(f"/api/hoja/{sid}/state", json={"session": "s9", "tab": "t1", "sel": {"r0": 2, "c0": 2, "r1": 3, "c1": 2}}).json() == {"open": True}
    assert surfaces.current("s9", "hoja")["sel"] == {"r0": 2, "c0": 2, "r1": 3, "c1": 2}
    assert "B2:B3 selected" in c.get(f"/api/hoja/{sid}/read", params={"session": "s9"}).json()["text"]
    q = c.post(f"/api/hoja/{sid}/query", json={"spec": {"column": "qty", "agg": "sum"}}).json()
    assert q["text"].endswith("= 10")
    assert c.post(f"/api/hoja/{sid}/state", json={"session": "s9", "open": False}).json() == {"open": False}
    xl = c.get(f"/api/hoja/{sid}/export.xlsx")
    assert xl.status_code == 200 and xl.content[:2] == b"PK" and xl.headers["content-disposition"].endswith('.xlsx"')
    assert c.get(f"/api/hoja/{sid}/export.csv").content.startswith(b"\xef\xbb\xbfitem,qty")
    up = c.post(f"/api/hoja/{sid}/import", params={"format": "xlsx"}, content=xl.content)
    assert up.status_code == 200
    assert c.post(f"/api/hoja/{sid}/import", params={"format": "xlsx"}, content=b"not a workbook").status_code == 400
    assert c.post(f"/api/hoja/{sid}/import", params={"format": "csv"}, content=b"x,y\n1,2\n").status_code == 200
    assert c.post(f"/api/hoja/{sid}/rename", json={"title": "R2"}).json()["title"] == "R2"
    assert c.delete(f"/api/hoja/{sid}").json() == {"deleted": True}
    assert c.get(f"/api/hoja/{sid}").status_code == 404


def test_registered_everywhere():
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    for n in NAMES:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    from src import tool_security
    assert set(NAMES) <= set(tool_security.NON_ADMIN_BLOCKED_TOOLS)
    from src.builtin_mcp import _BUILTIN_SERVERS, NATIVE_TWIN_SERVERS
    assert "sheet" in _BUILTIN_SERVERS and "sheet" in NATIVE_TWIN_SERVERS


def test_tools_end_to_end():
    out = _tool("sheet_create", {"title": "Tracker", "rows": [["Task", "Hours", "Rate"], ["a", 2, 30], ["b", 3, 30]],
                                 "ops": None})
    assert out["exit_code"] == 0 and "created" in out["output"]
    sid = out["result"]["id"]
    w = _tool("sheet_write", {"ops": [{"op": "set", "at": "D1", "values": [["Cost"], ["=B2*C2"], ["=SUMA(D2:D2)*0+B3*C3"]]},
                                     {"op": "format", "range": "D2:D3", "format": "currency", "decimals": 0},
                                     {"op": "chart", "type": "bar", "range": "A1:B3", "title": "Hours"}]})
    assert w["exit_code"] == 0 and "rev" in w["output"], w
    r = _tool("sheet_read", {})
    assert "Task" in r["output"] and "$60" in r["output"] and "{=B2*C2}" in r["output"]
    q = _tool("sheet_query", {"column": "Hours", "agg": "sum"})
    assert q["output"].endswith("= 5")
    fail = _tool("sheet_write", {"ops": [{"op": "set", "at": "A9", "values": [["ok"]]}, {"op": "nosuch"}]})
    assert fail["exit_code"] == 1 and "wrote 1 cells" in fail["error"]
    assert _tool("sheet_read", {"id": "nope"})["exit_code"] == 1
    assert _tool("sheet_read", {}, session="other")["exit_code"] == 1       # a chat without a sheet is told to create one
    assert hoja.get(sid)[0]["rev"] >= 3


def test_agent_write_tells_the_window_open_cells_and_cursor():
    actor = {"id": "s1", "name": "Faustus", "color": "#ef5f00"}

    async def go():
        meta = hoja.create("t", session_id="s1")
        sub = surfaces.subscribe("s1")
        await surfaces.sheet_write("s1", meta["id"], [{"op": "set", "at": "A1", "values": [[1, 2], [3, 4]]}], actor)
        await asyncio.sleep(0.05)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        surfaces.unsubscribe(sub)
        return meta, events

    meta, events = run(go())
    kinds = [e["event"] for e in events]
    assert kinds[0] == "open" and events[0]["data"]["focus"] is False
    cells = events[kinds.index("cells")]["data"]
    assert cells["origin"] == "agent" and cells["id"] == meta["id"] and cells["tabs"]
    cursors = [e["data"] for e in events if e["event"] == "cursor"]
    assert cursors and all(c["id"] == meta["id"] and c["kind"] == "hoja" for c in cursors)
    assert cursors[0]["state"] == "working" and "r" in cursors[0] and "c" in cursors[0] and cursors[-1]["state"] == "idle"


def test_context_block_names_the_sheet_and_the_selection():
    meta = hoja.create("Budget", session_id="s3")
    hoja.edit(meta["id"], [{"op": "set", "at": "A1", "values": [["x", 5]]}])
    tab = hoja.get(meta["id"])[1].tabs[0].id
    surfaces.report("s3", "hoja", meta["id"], tab=tab, sel={"r0": 1, "c0": 1, "r1": 1, "c1": 2})
    block = surfaces.context_block("s3")
    assert "Spreadsheet open beside the chat" in block and "A1:B1" in block and len(block) < 800


def test_mcp_server_lists_the_tools_with_a_session():
    from mcp_servers import sheet_server
    tools = run(sheet_server.list_tools())
    assert [t.name for t in tools] == NAMES
    for t in tools:
        assert t.inputSchema["required"][0] == "session"
    assert run(sheet_server.call_tool("sheet_read", {})).isError is True


def test_the_sheet_tool_descriptions_are_short():
    for s in tool_schemas.FUNCTION_TOOL_SCHEMAS:
        fn = s["function"]
        if fn["name"] in NAMES:
            assert len(fn["description"]) + len(str(fn["parameters"])) < 3500
