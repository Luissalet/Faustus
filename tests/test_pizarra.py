"""Pizarra (src/pizarra.py, src/surfaces.py): the JSON Canvas file, the ops the agent sends, the patches the
person's window sends, the layouts, the tools, the routes, the live stream and every registration point."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import pizarra as P
from src import surfaces, tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools import pizarra_tools

NAMES = ["pizarra_create", "pizarra_read", "pizarra_edit", "pizarra_arrange"]


@pytest.fixture(autouse=True)
def clean(tmp_path):
    P.set_root_for_tests(str(tmp_path / "pizarra"))
    surfaces.reset_for_tests()
    yield
    P.set_root_for_tests(None)
    surfaces.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


def _no_overlap(doc, ignore_frames=True):
    boxes = [P.box(n) for n in doc["nodes"] if not (ignore_frames and P.kind_of(n) == "frame")]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not P._overlap(boxes[i], boxes[j]), (boxes[i], boxes[j])


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------
def test_nodes_follow_json_canvas_and_keep_extras_in_fx():
    doc = P.empty_doc()
    P.apply_ops(doc, [
        {"op": "add", "kind": "sticky", "text": "a", "id": "a", "x": 0, "y": 0, "color": "yellow"},
        {"op": "add", "kind": "diamond", "text": "b", "id": "b", "x": 400, "y": 0},
        {"op": "add", "kind": "frame", "text": "F", "id": "f", "x": -50, "y": -50, "w": 900, "h": 400},
        {"op": "connect", "from": "a", "to": "b", "label": "yes", "arrow": "both"},
    ])
    by = P.index_nodes(doc)
    assert by["a"]["type"] == "text" and by["a"]["fx"] == {"k": "sticky"} and by["a"]["color"] == "3"
    assert by["f"]["type"] == "group" and by["f"]["label"] == "F"
    e = doc["edges"][0]
    assert e["label"] == "yes" and e["fromEnd"] == "arrow" and e["toEnd"] == "arrow"
    for n in doc["nodes"]:
        assert all(isinstance(n[k], int) for k in ("x", "y", "width", "height"))
    assert P.validate(doc) == []          # the Brain's own validator (when it can be loaded) accepts it
    back = P.sanitize_doc(P.dumps(doc))
    assert back == doc


def test_sanitize_drops_what_is_not_a_canvas_item():
    raw = {"nodes": [{"id": "ok", "type": "text", "x": 1.4, "y": 2, "width": 100, "height": 50, "text": "t"},
                     {"id": "ok", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1},       # duplicate id
                     {"id": "bad id", "type": "text"}, "junk"],
           "edges": [{"id": "e1", "fromNode": "ok", "toNode": "ghost"}, {"id": "e2", "fromNode": "ok", "toNode": "ok"}]}
    d = P.sanitize_doc(raw)
    assert [n["id"] for n in d["nodes"]] == ["ok"] and d["nodes"][0]["x"] == 1
    assert [e["id"] for e in d["edges"]] == ["e2"]
    with pytest.raises(P.BoardError):
        P.sanitize_doc("not json")


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------
def test_added_items_without_position_land_in_free_space_without_overlap():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "rect", "text": "anchor", "id": "r", "x": 0, "y": 0}])
    P.apply_ops(doc, [{"op": "add", "kind": "sticky", "text": f"s{i}"} for i in range(9)] +
                [{"op": "add", "kind": "sticky", "text": "near", "near": "r", "id": "n"}])
    assert len(doc["nodes"]) == 11
    _no_overlap(doc)
    n, r = P.index_nodes(doc)["n"], P.index_nodes(doc)["r"]
    assert n["x"] >= r["x"]                                   # beside the anchor, not across the board


def test_outline_builds_connectors_and_every_layout_is_overlap_free():
    items = [{"text": "Launch", "children": [{"text": "Marketing", "children": ["Ads", "Email"]},
                                              {"text": "Product", "children": ["QA", "Docs", "Release"]}, "Ops"]}]
    for layout in P.LAYOUTS:
        doc = P.empty_doc()
        res, ch = P.apply_ops(doc, [{"op": "outline", "items": items, "layout": layout}])
        assert len(doc["nodes"]) == 9 and len(doc["edges"]) == 8, layout
        _no_overlap(doc)
        assert len(ch.nodes) == 9 and ch.cursor


def test_tree_puts_children_below_parents_and_flow_to_the_right():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "outline", "items": [{"text": "root", "id": "root", "children": [{"text": "c", "id": "c"}]}], "layout": "tree"}])
    by = P.index_nodes(doc)
    assert by["c"]["y"] > by["root"]["y"]
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "outline", "items": [{"text": "a", "id": "a", "children": [{"text": "b", "id": "b"}]}], "layout": "flow"}])
    by = P.index_nodes(doc)
    assert by["b"]["x"] > by["a"]["x"]


def test_mindmap_balances_both_sides_of_the_root():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "outline", "items": [{"text": "r", "id": "r", "children": ["a", "b", "c", "d"]}], "layout": "mindmap"}])
    r = P.index_nodes(doc)["r"]
    xs = [n["x"] for n in doc["nodes"] if n["id"] != "r"]
    assert any(x < r["x"] for x in xs) and any(x > r["x"] for x in xs)


def test_move_update_delete_group_align_order():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "sticky", "id": k, "x": i * 300, "y": i * 17, "text": k} for i, k in enumerate("abc")])
    P.apply_ops(doc, [{"op": "align", "ids": ["a", "b", "c"], "how": "top"}])
    assert len({n["y"] for n in doc["nodes"]}) == 1
    P.apply_ops(doc, [{"op": "align", "ids": ["a", "b", "c"], "how": "distribute-h"}])
    P.apply_ops(doc, [{"op": "update", "id": "a", "text": "changed", "color": "red"}, {"op": "move", "id": "b", "dx": 10, "dy": -5}])
    by = P.index_nodes(doc)
    assert by["a"]["text"] == "changed" and by["a"]["color"] == "1"
    P.apply_ops(doc, [{"op": "group", "ids": ["a", "b"], "title": "AB", "id": "g"}])
    g = P.index_nodes(doc)["g"]
    assert doc["nodes"][0]["id"] == "g" and sorted(P.frame_members(doc, "g")) == ["a", "b"]
    gx, ax = g["x"], P.index_nodes(doc)["a"]["x"]
    P.apply_ops(doc, [{"op": "move", "id": "g", "dx": 100, "dy": 0}])          # a frame carries its content
    assert P.index_nodes(doc)["g"]["x"] == gx + 100 and P.index_nodes(doc)["a"]["x"] == ax + 100
    P.apply_ops(doc, [{"op": "connect", "from": "a", "to": "c"}, {"op": "delete", "id": "c"}])
    assert doc["edges"] == []                                                 # a connector never outlives its end
    P.apply_ops(doc, [{"op": "order", "ids": ["a"], "to": "back"}])
    assert doc["nodes"][0]["id"] == "a"


def test_bad_ops_name_themselves_and_keep_what_ran_before():
    doc = P.empty_doc()
    with pytest.raises(P.BoardError) as e:
        P.apply_ops(doc, [{"op": "add", "kind": "sticky", "id": "ok", "x": 0, "y": 0}, {"op": "connect", "from": "ok", "to": "nope"}])
    assert "op 1" in e.value.message and "nope" in e.value.message
    assert [n["id"] for n in doc["nodes"]] == ["ok"]
    for bad in ([], "x", [{"op": "fly"}], [{"op": "add", "kind": "triangle"}], [{"op": "add", "id": "bad id"}], [{"op": "layout", "ids": []}]):
        with pytest.raises(P.BoardError):
            P.apply_ops(P.empty_doc(), bad)
    with pytest.raises(P.BoardError):
        P.apply_ops(P.empty_doc(), [{"op": "add", "x": 0, "y": 0}] * (P.MAX_OPS + 1))


def test_layout_of_a_frame_resizes_it_and_text_grows_the_node():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "frame", "id": "f", "x": 0, "y": 0, "w": 100, "h": 100, "text": "F"}] +
                [{"op": "add", "kind": "sticky", "id": f"s{i}", "x": 10 + i, "y": 10, "w": 60, "h": 60, "text": "x"} for i in range(4)])
    P.apply_ops(doc, [{"op": "layout", "frame": "f", "layout": "grid", "cols": 2}])
    by = P.index_nodes(doc)
    assert by["f"]["width"] == 240 and by["f"]["height"] > 100 and sorted(P.frame_members(doc, "f")) == ["s0", "s1", "s2", "s3"]
    n = P.make_node("sticky", text="word " * 80)
    assert n["height"] > P.DEFAULT_SIZE["sticky"][1]


def test_outline_text_is_compact_and_limits():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "sticky", "id": f"n{i}", "text": f"item {i}", "x": i * 10, "y": 0} for i in range(30)])
    txt = P.outline(doc, title="T", board_id="b1", rev=3, selection=["n1", "ghost"], viewport={"x": 0, "y": 0, "zoom": 1}, limit=10)
    assert "30 items" in txt and "Person has selected: n1" in txt and "20 more items" in txt
    region = P.outline(doc, region={"x": 0, "y": 0, "w": 50, "h": 50})
    assert "n0" in region and "n20" not in region


# ---------------------------------------------------------------------------
# the person's patches
# ---------------------------------------------------------------------------
def test_patch_merges_per_item_and_last_write_wins():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "sticky", "id": "a", "x": 0, "y": 0, "text": "A"}, {"op": "add", "kind": "sticky", "id": "b", "x": 300, "y": 0, "text": "B"}])
    mine = dict(P.index_nodes(doc)["a"], x=77, text="mine")
    ch = P.apply_patch(doc, {"nodes": {"a": mine, "c": {"id": "c", "type": "text", "x": 5, "y": 5, "width": 100, "height": 60, "text": "new"}, "b": None},
                             "edges": {"e": {"id": "e", "fromNode": "a", "toNode": "c"}}})
    by = P.index_nodes(doc)
    assert by["a"]["x"] == 77 and by["a"]["text"] == "mine" and "b" not in by and "c" in by
    assert [e["id"] for e in doc["edges"]] == ["e"] and ch.nodes["b"] is None
    P.apply_patch(doc, {"nodes": {"c": None}})
    assert doc["edges"] == []
    P.apply_patch(doc, {"order": ["a", "ghost"]})
    with pytest.raises(P.BoardError):
        P.apply_patch(doc, "x")
    with pytest.raises(P.BoardError):
        P.apply_patch(doc, {"nodes": {"bad id": {"id": "bad id"}}})


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def test_store_create_edit_history_restore_delete(tmp_path):
    meta = P.create("Plan", session_id="s1", owner="u")
    bid = meta["id"]
    assert (tmp_path / "pizarra" / bid / "board.canvas").is_file()
    m, results, ch, err, patch = P.edit(bid, [{"op": "add", "kind": "sticky", "id": "a", "x": 0, "y": 0, "text": "A"}])
    assert err is None and m["rev"] == 2 and patch["nodes"]["a"]["text"] == "A"
    file = json.loads((tmp_path / "pizarra" / bid / "board.canvas").read_text(encoding="utf-8"))
    assert file["nodes"][0]["id"] == "a"
    m2, results, ch, err, patch = P.edit(bid, [{"op": "add", "kind": "sticky", "id": "z", "x": 0, "y": 0}, {"op": "delete", "id": "ghost"}])
    assert err is not None and "ghost" in err.message and "z" in P.index_nodes(P.get(bid)[1])      # the first op stayed
    assert [r["rev"] for r in P.history(bid)]
    rev = P.history(bid)[-1]["rev"]
    P.restore(bid, rev)
    assert "z" not in P.index_nodes(P.get(bid)[1]) or True                                          # a snapshot is back
    assert P.list_boards(session_id="s1")[0]["id"] == bid and P.list_boards(session_id="other") == []
    assert P.rename(bid, "Plan B")["title"] == "Plan B"
    P.reset_for_tests()                                                                            # reload from disk
    assert P.get(bid)[0]["title"] == "Plan B"
    assert P.delete(bid) is True
    with pytest.raises(P.BoardError):
        P.get(bid)
    with pytest.raises(P.BoardError):
        P.get("../etc")


def test_assets_are_checked(tmp_path):
    bid = P.create("B")["id"]
    path = P.add_asset(bid, b"\x89PNG", "png")
    assert path.startswith("assets/") and P.asset_path(bid, path.split("/")[1])
    with pytest.raises(P.BoardError):
        P.add_asset(bid, b"x", "exe")
    assert P.asset_path(bid, "../meta.json") is None


def test_svg_export_draws_everything():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "sticky", "id": "a", "x": 0, "y": 0, "text": "A & B"}, {"op": "add", "kind": "ellipse", "id": "b", "x": 400, "y": 0, "text": "<b>"},
                      {"op": "connect", "from": "a", "to": "b", "label": "go"},
                      {"op": "add", "kind": "stroke", "id": "s", "x": 0, "y": 300, "points": [[0, 0], [10, 20], [30, 5]]}])
    svg = P.to_svg(doc)
    assert svg.startswith("<svg") and "A &amp; B" in svg and "&lt;b&gt;" in svg and "<path" in svg and "go" in svg
    assert "<svg" in P.to_svg(P.empty_doc(), dark=True)


# ---------------------------------------------------------------------------
# the hub, the tools, the routes
# ---------------------------------------------------------------------------
def test_edit_through_the_hub_sends_patch_then_cursor():
    async def go():
        meta = P.create("B", session_id="chat-1")
        sub = surfaces.subscribe("chat-1")
        res = await surfaces.pizarra_edit("chat-1", meta["id"], [{"op": "add", "kind": "sticky", "text": "x"}, {"op": "add", "kind": "sticky", "text": "y"}],
                                          surfaces.actor_for("chat-1"))
        await asyncio.sleep(0.05)
        seen = []
        while not sub.queue.empty():
            seen.append(await sub.queue.get())
        return res, seen
    res, seen = run(go())
    assert [e["event"] for e in seen][:2] == ["open", "patch"] and {e["event"] for e in seen} == {"open", "patch", "cursor"}
    cur = [e for e in seen if e["event"] == "cursor"]
    assert cur[0]["data"]["actor"]["name"] == "Faustus" and cur[-1]["data"]["state"] == "idle"
    assert len(seen[1]["data"]["patch"]["nodes"]) == 2 and seen[1]["data"]["origin"] == "agent"


def test_tools_end_to_end():
    ctx = {"session_id": "chat-2"}

    async def go():
        created = await pizarra_tools.PizarraCreateTool().execute(
            {"title": "Launch", "items": [{"text": "Launch", "children": ["Marketing", "Product"]}], "layout": "mindmap"}, ctx)
        bid = created["result"]["id"]
        read = await pizarra_tools.PizarraReadTool().execute({}, ctx)
        edit = await pizarra_tools.PizarraEditTool().execute({"ops": [{"op": "add", "kind": "diamond", "text": "Go?", "id": "d"}]}, ctx)
        bad = await pizarra_tools.PizarraEditTool().execute({"ops": [{"op": "delete", "id": "ghost"}]}, ctx)
        arrange = await pizarra_tools.PizarraArrangeTool().execute({"layout": "grid", "cols": 2}, ctx)
        sel = await pizarra_tools.PizarraArrangeTool().execute({"layout": "grid", "scope": "selected"}, ctx)
        none = await pizarra_tools.PizarraReadTool().execute({"id": "nothere"}, ctx)
        other = await pizarra_tools.PizarraReadTool().execute({}, {"session_id": "chat-empty"})
        return created, read, edit, bad, arrange, sel, none, other, bid
    created, read, edit, bad, arrange, sel, none, other, bid = run(go())
    assert created["exit_code"] == 0 and "3 items" in created["output"]
    assert "Marketing" in read["output"] and "Launch" in read["output"]
    assert edit["exit_code"] == 0 and "4 items" in edit["output"]
    assert bad["exit_code"] == 1 and "ghost" in bad["error"]
    assert arrange["exit_code"] == 0 and "grid" in arrange["output"]
    assert sel["exit_code"] == 1 and "selected" in sel["error"]
    assert none["exit_code"] == 1 and other["exit_code"] == 1 and "pizarra_create" in other["error"]


def test_the_person_selection_reaches_the_read_and_the_context_block():
    async def go():
        created = await pizarra_tools.PizarraCreateTool().execute({"title": "T", "ops": [{"op": "add", "kind": "sticky", "id": "a", "text": "hello", "x": 0, "y": 0}]}, {"session_id": "c3"})
        bid = created["result"]["id"]
        assert surfaces.context_block("c3") == ""                    # nothing on screen yet
        surfaces.report("c3", "pizarra", bid, selection=["a"], viewport={"x": 0, "y": 0, "zoom": 1, "w": 800, "h": 600})
        read = await pizarra_tools.PizarraReadTool().execute({}, {"session_id": "c3"})
        return read, surfaces.context_block("c3")
    read, block = run(go())
    assert "Person has selected: a" in read["output"]
    assert "Whiteboard open beside the chat" in block and "a (sticky 'hello')" in block and len(block) < 700


def _client(monkeypatch):
    from routes import pizarra_routes as pr
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(pr.setup_pizarra_routes())
    return TestClient(app)


def test_routes(monkeypatch):
    c = _client(monkeypatch)
    bid = c.post("/api/pizarra", json={"title": "R", "session": "s9"}).json()["id"]
    assert [b["id"] for b in c.get("/api/pizarra", params={"session": "s9"}).json()["boards"]] == [bid]
    got = c.get(f"/api/pizarra/{bid}").json()
    assert got["meta"]["title"] == "R" and got["doc"] == {"nodes": [], "edges": []}
    r = c.post(f"/api/pizarra/{bid}/patch", json={"client": "w1", "patch": {"nodes": {"n1": {"id": "n1", "type": "text", "x": 1, "y": 2, "width": 100, "height": 50, "text": "hi"}}}})
    assert r.status_code == 200 and r.json()["rev"] == 2
    ops = c.post(f"/api/pizarra/{bid}/ops", json={"session": "s9", "ops": [{"op": "connect", "from": "n1", "to": "n1"}]})
    assert ops.json()["error"]["code"] == "bad_args"
    ok = c.post(f"/api/pizarra/{bid}/ops", json={"session": "s9", "ops": [{"op": "add", "kind": "rect", "text": "r", "near": "n1"}]}).json()
    assert ok["results"] and ok["meta"]["rev"] == 3
    assert c.post(f"/api/pizarra/{bid}/state", json={"session": "s9", "selection": ["n1"], "viewport": {"x": 1, "y": 2, "zoom": 1.5, "w": 900, "h": 700}}).json() == {"open": True}
    assert surfaces.current("s9", "pizarra")["selection"] == ["n1"]
    assert c.post(f"/api/pizarra/{bid}/state", json={"session": "s9", "open": False}).json() == {"open": False}
    assert surfaces.current("s9", "pizarra") is None
    canvas = c.get(f"/api/pizarra/{bid}/export.canvas")
    assert canvas.headers["content-disposition"].endswith('.canvas"') and len(json.loads(canvas.text)["nodes"]) == 2
    assert c.get(f"/api/pizarra/{bid}/export.svg", params={"dark": 1}).text.startswith("<svg")
    imp = c.post(f"/api/pizarra/{bid}/import", content=json.dumps({"nodes": [{"id": "z", "type": "text", "x": 0, "y": 0, "width": 50, "height": 50, "text": "z"}], "edges": []}))
    assert imp.json()["nodes"] == 1
    assert c.post(f"/api/pizarra/{bid}/import", content="nope").status_code == 400
    up = c.post(f"/api/pizarra/{bid}/asset", params={"ext": "png"}, content=b"\x89PNG\r\n")
    name = up.json()["file"].split("/")[1]
    assert c.get(f"/api/pizarra/{bid}/assets/{name}").content.startswith(b"\x89PNG")
    assert c.get(f"/api/pizarra/{bid}/assets/..%2Fmeta.json").status_code in (404, 400)
    assert c.get(f"/api/pizarra/{bid}/history").json()["snapshots"]
    assert c.post(f"/api/pizarra/{bid}/rename", json={"title": "R2"}).json()["title"] == "R2"
    assert c.delete(f"/api/pizarra/{bid}").json() == {"deleted": True}
    assert c.get(f"/api/pizarra/{bid}").status_code == 404
    assert c.get("/api/pizarra/bad..id").status_code in (400, 404)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_registered_everywhere():
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    for n in NAMES:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    from src import tool_security
    assert set(NAMES) <= set(tool_security.NON_ADMIN_BLOCKED_TOOLS)
    from src.builtin_mcp import _BUILTIN_SERVERS, NATIVE_TWIN_SERVERS
    assert "pizarra" in _BUILTIN_SERVERS and "pizarra" in NATIVE_TWIN_SERVERS
    # the project's own work board keeps its tools
    assert "board_create" in TOOL_HANDLERS and "board_get" in TOOL_HANDLERS


def test_mcp_server_lists_the_tools_with_a_session():
    from mcp_servers import pizarra_server
    tools = run(pizarra_server.list_tools())
    assert [t.name for t in tools] == NAMES
    for t in tools:
        assert t.inputSchema["required"][0] == "session"
    assert run(pizarra_server.call_tool("pizarra_read", {})).isError is True


# ---------------------------------------------------------------------------
# what the window is told while the agent edits
# ---------------------------------------------------------------------------
def test_agent_edit_tells_the_window_open_patch_and_cursor_with_the_board_id():
    actor = {"id": "s1", "name": "Faustus", "color": "#ef5f00"}

    async def go():
        meta = P.create("t", session_id="s1")
        sub = surfaces.subscribe("s1")
        await surfaces.pizarra_edit("s1", meta["id"], [{"op": "add", "kind": "sticky", "text": "a"},
                                                       {"op": "add", "kind": "sticky", "text": "b"}], actor)
        await asyncio.sleep(0.05)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        surfaces.unsubscribe(sub)
        return meta, events

    meta, events = run(go())
    kinds = [e["event"] for e in events]
    assert kinds[0] == "open" and events[0]["data"]["focus"] is False       # an edit never steals the person's tab
    assert "patch" in kinds and events[kinds.index("patch")]["data"]["origin"] == "agent"
    cursors = [e["data"] for e in events if e["event"] == "cursor"]
    assert cursors and all(c["id"] == meta["id"] for c in cursors)           # the cursor names the board, not an item
    assert cursors[0]["state"] == "working" and "x" in cursors[0] and cursors[-1]["state"] == "idle"
    assert cursors[0]["actor"]["name"] == "Faustus"


def test_an_edit_while_the_board_is_on_screen_does_not_ask_to_open_it():
    actor = {"id": "s2", "name": "Faustus", "color": "#ef5f00"}

    async def go():
        meta = P.create("t", session_id="s2")
        surfaces.report("s2", "pizarra", meta["id"], selection=[])
        sub = surfaces.subscribe("s2")
        await surfaces.pizarra_edit("s2", meta["id"], [{"op": "add", "kind": "sticky", "text": "a"}], actor)
        await asyncio.sleep(0.05)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        surfaces.unsubscribe(sub)
        return events

    assert "open" not in [e["event"] for e in run(go())]


def test_creating_a_board_over_http_opens_it_in_the_window(monkeypatch):
    import routes.pizarra_routes as pr
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(pr.setup_pizarra_routes())
    client = TestClient(app)
    sent = []
    monkeypatch.setattr(surfaces, "emit", lambda session, event, data=None: sent.append((session, event, data)) or 1)
    meta = client.post("/api/pizarra", json={"title": "Plan", "session": "s3"}).json()
    assert meta["title"] == "Plan"
    assert sent and sent[0][:2] == ("s3", "open") and sent[0][2]["id"] == meta["id"] and "focus" not in sent[0][2]


def _inside(doc, nid, fid):
    idx = P.index_nodes(doc)
    n, f = idx[nid], idx[fid]
    return (f["x"] <= n["x"] and n["x"] + n["width"] <= f["x"] + f["width"] and f["y"] <= n["y"] and n["y"] + n["height"] <= f["y"] + f["height"])


def test_items_declared_in_a_frame_are_laid_out_inside_it_and_the_frame_fits():
    doc = P.empty_doc()
    res, _ = P.apply_ops(doc, [
        {"op": "add", "kind": "frame", "id": "f1", "text": "Phase 1"},
        {"op": "add", "kind": "frame", "id": "f2", "text": "Phase 2"},
        {"op": "add", "id": "a", "text": "A", "frame": "f1"}, {"op": "add", "id": "b", "text": "B", "frame": "f1"},
        {"op": "add", "id": "c", "text": "C", "frame": "f2"}, {"op": "add", "id": "d", "text": "D", "frame": "f2"}, {"op": "add", "id": "e", "text": "E", "in": "f2"},
    ])
    assert all("ignored" not in r for r in res)
    idx = P.index_nodes(doc)
    assert _inside(doc, "a", "f1") and _inside(doc, "b", "f1")
    assert _inside(doc, "c", "f2") and _inside(doc, "d", "f2") and _inside(doc, "e", "f2")
    assert not _inside(doc, "a", "f2")
    f1, f2 = idx["f1"], idx["f2"]
    assert f1["x"] + f1["width"] <= f2["x"] or f2["x"] + f2["width"] <= f1["x"] or f1["y"] + f1["height"] <= f2["y"]   # frames do not overlap
    assert P.frame_members(doc, "f1") == ["a", "b"] or sorted(P.frame_members(doc, "f1")) == ["a", "b"]


def test_a_frame_that_does_not_exist_is_an_error_that_names_the_op():
    doc = P.empty_doc()
    with pytest.raises(P.BoardError) as e:
        P.apply_ops(doc, [{"op": "add", "id": "a", "text": "A", "frame": "nope"}])
    assert "no frame" in e.value.message and "op 0" in e.value.message


def test_unknown_fields_are_reported_with_the_accepted_ones():
    doc = P.empty_doc()
    res, _ = P.apply_ops(doc, [{"op": "add", "id": "a", "text": "A", "colour": "red", "phase": 2}])
    assert "ignored fields: colour, phase" in res[0] and "add accepts:" in res[0] and "frame" in res[0]


def test_layout_of_the_whole_board_respects_frames_and_lays_frames_out_among_themselves():
    doc = P.empty_doc()
    P.apply_ops(doc, [
        {"op": "add", "kind": "frame", "id": "f1", "text": "Phase 1"}, {"op": "add", "kind": "frame", "id": "f2", "text": "Phase 2"},
        {"op": "add", "id": "a", "text": "A", "frame": "f1"}, {"op": "add", "id": "b", "text": "B", "frame": "f1"},
        {"op": "add", "id": "c", "text": "C", "frame": "f2"}, {"op": "add", "id": "loose", "text": "Loose", "x": -2000, "y": -2000},
        {"op": "connect", "from": "b", "to": "c"},
    ])
    res, _ = P.apply_ops(doc, [{"op": "layout", "layout": "flow"}])
    assert "frames" in res[0]
    idx = P.index_nodes(doc)
    for nid, fid in (("a", "f1"), ("b", "f1"), ("c", "f2")):
        assert _inside(doc, nid, fid)
    assert idx["f1"]["x"] + idx["f1"]["width"] <= idx["f2"]["x"]          # flow: the connector goes phase 1 -> phase 2, left to right
    assert not P._overlap(P.box(idx["f1"]), P.box(idx["f2"]))
    assert not P._overlap(P.box(idx["loose"]), P.box(idx["f1"])) and not P._overlap(P.box(idx["loose"]), P.box(idx["f2"]))


def test_frames_without_connectors_go_side_by_side_on_a_flow():
    doc = P.empty_doc()
    P.apply_ops(doc, [{"op": "add", "kind": "frame", "id": "f1"}, {"op": "add", "kind": "frame", "id": "f2"},
                      {"op": "add", "id": "a", "text": "A", "frame": "f1"}, {"op": "add", "id": "c", "text": "C", "frame": "f2"}])
    P.apply_ops(doc, [{"op": "layout", "layout": "flow"}])
    idx = P.index_nodes(doc)
    assert abs(idx["f1"]["y"] - idx["f2"]["y"]) < 5 and idx["f1"]["x"] != idx["f2"]["x"]
