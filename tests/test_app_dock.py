"""App Dock (src/app_dock.py): state per chat, the call rendezvous with its timeout and `no_host`, the
routes, the three agent tools, the turn's context block, `changed`, and every registration point."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import app_dock, tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools import app_dock_tools

NAMES = ["app_open", "app_view", "app_act"]
APPS = [
    {"id": "darwin", "name": "Darwin's Hoard", "url": "http://127.0.0.1:5233", "state": "running", "icon": True,
     "purpose": "discoveries", "launchable": True, "bridge": True},
    {"id": "links", "name": "Links Hoard", "url": "http://127.0.0.1:5181", "state": "stopped", "icon": True,
     "purpose": "bookmarks", "launchable": True, "bridge": False},
]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    app_dock.reset_for_tests()
    monkeypatch.setattr(app_dock, "list_apps", lambda: {"apps": [dict(a) for a in APPS], "hub": "http://hub", "available": True})
    monkeypatch.setattr(app_dock, "start_through_hub", lambda app_id, timeout=90.0: True)
    yield
    app_dock.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def test_state_dock_report_close():
    assert app_dock.get("s1")["app_id"] is None
    app_dock.dock("s1", "darwin", "Darwin's Hoard", "http://127.0.0.1:5233")
    snap = app_dock.get("s1")
    assert snap["app_id"] == "darwin" and snap["ready"] is False and snap["tools"] == [] and snap["updated"]
    app_dock.report("s1", ready=True, tools=[{"name": "open_video", "description": "x" * 900, "inputSchema": {"type": "object"}},
                                             {"nope": 1}, "junk"],
                    context={"title": "Videos", "url": "/#/v", "focus": {"kind": "video", "id": "3df9", "label": "A", "x": 1},
                             "selection": "s" * 900, "evil": "dropped"}, app={"id": "x", "name": "Darwin"})
    snap = app_dock.get("s1")
    assert snap["ready"] is True and snap["name"] == "Darwin" and snap["app_id"] == "darwin"
    assert [t["name"] for t in snap["tools"]] == ["open_video"] and len(snap["tools"][0]["description"]) == 300
    assert snap["context"]["focus"] == {"kind": "video", "id": "3df9", "label": "A"}
    assert len(snap["context"]["selection"]) == 500 and "evil" not in snap["context"]
    assert app_dock.get("s2")["app_id"] is None                        # per chat
    assert app_dock.close("s1")["closed"] is True and app_dock.get("s1")["app_id"] is None
    assert app_dock.close("s1")["closed"] is False
    with pytest.raises(app_dock.DockError):
        app_dock.report("s1", ready=True)
    with pytest.raises(app_dock.DockError):
        app_dock.dock("", "a", "b", "c")


def test_dock_and_close_emit_to_host():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin's Hoard", "http://x")
        app_dock.close("s1")
        a, b = await sub.queue.get(), await sub.queue.get()
        return a, b
    a, b = run(go())
    assert a["event"] == "open" and a["data"]["app"] == "darwin" and a["data"]["url"] == "http://x"
    assert b["event"] == "close"


def test_actor_is_stable_and_sub_agents_get_their_own(monkeypatch):
    a1, a2 = app_dock.actor_for("chat-1"), app_dock.actor_for("chat-1")
    assert a1 == a2 and a1["name"] == "Faustus" and a1["id"] == "chat-1" and a1["color"].startswith("#")
    runs = {"child-1": SimpleNamespace(name="Researcher", parent_session_id="chat-1"),
            "child-2": SimpleNamespace(name="Writer", parent_session_id="child-1")}
    monkeypatch.setattr(app_dock, "_worker_run", lambda sid: runs.get(sid))
    c = app_dock.actor_for("child-1")
    assert c["name"] == "Researcher" and c["id"] == "child-1" and c["id"] != a1["id"]
    # the dock a sub-agent uses is the one of the nearest chat that has one
    assert app_dock.resolve_session("child-2") == "chat-1"            # nothing docked: the top of the chain
    app_dock.dock("chat-1", "darwin", "Darwin", "http://x")
    assert app_dock.resolve_session("child-2") == "chat-1"
    assert app_dock.resolve_session("other") == "other"
    colors = {app_dock.color_for(f"id{i}") for i in range(40)}
    assert len(colors) > 3


# ---------------------------------------------------------------------------
# the rendezvous
# ---------------------------------------------------------------------------
def test_call_round_trip():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        assert (await sub.queue.get())["event"] == "open"
        actor = {"id": "a", "name": "Faustus", "color": "#111111"}
        task = asyncio.create_task(app_dock.call("s1", "app_click", {"text": "Save"}, actor))
        ev = await sub.queue.get()
        assert ev["event"] == "call" and ev["data"]["tool"] == "app_click" and ev["data"]["actor"] == actor
        assert ev["data"]["args"] == {"text": "Save"}
        assert app_dock.pending_count() == 1
        assert app_dock.resolve("other-session", ev["data"]["id"], True, {}) is False       # only the session's own host answers
        assert app_dock.resolve("s1", ev["data"]["id"], True, {"clicked": "Save"}) is True
        out = await task
        assert app_dock.pending_count() == 0
        assert app_dock.resolve("s1", ev["data"]["id"], True, {}) is False                  # answered once
        return out
    assert run(go()) == {"ok": True, "result": {"clicked": "Save"}}


def test_call_error_answer_keeps_code_and_candidates():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        await sub.queue.get()
        task = asyncio.create_task(app_dock.call("s1", "app_click", {"text": "Guardar"}))
        ev = await sub.queue.get()
        app_dock.resolve("s1", ev["data"]["id"], False, error={"code": "not_found", "message": "no such control", "nearest": ["Guardar todo"]})
        return await task
    out = run(go())
    assert out["ok"] is False and out["error"]["code"] == "not_found" and out["error"]["nearest"] == ["Guardar todo"]


def test_call_times_out():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        out = await app_dock.call("s1", "app_click", {}, timeout=0.15)
        assert app_dock.pending_count() == 0
        return out, sub
    out, _ = run(go())
    assert out["ok"] is False and out["error"]["code"] == "timeout"


def test_no_host_answers_at_once_and_no_app_and_not_ready(monkeypatch):
    monkeypatch.setattr(app_dock, "READY_WAIT_S", 0.2)
    async def go():
        t0 = asyncio.get_running_loop().time()
        none = await app_dock.call("s1", "app_read")                     # nothing docked
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        nohost = await app_dock.call("s1", "app_read")                   # docked, nobody listening
        took = asyncio.get_running_loop().time() - t0
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")              # re-dock resets readiness
        waiting = await app_dock.wait_ready("s1", 0.2)
        notready = await app_dock.call("s1", "app_read")
        return none, nohost, took, waiting, notready, sub
    none, nohost, took, waiting, notready, _ = run(go())
    assert none["error"]["code"] == "no_app"
    assert nohost["error"]["code"] == "no_host" and "app_open" in nohost["error"]["message"]
    assert took < 1.0                                                    # never the 45 s wait
    assert waiting is False and notready["error"]["code"] == "not_ready"


def test_host_leaving_ends_calls_in_flight():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        task = asyncio.create_task(app_dock.call("s1", "app_read", timeout=5))
        await asyncio.sleep(0.05)
        app_dock.unsubscribe(sub)
        return await asyncio.wait_for(task, 1.0)
    assert run(go())["error"]["code"] == "no_host"


def test_closing_ends_calls_in_flight():
    async def go():
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        task = asyncio.create_task(app_dock.call("s1", "app_read", timeout=5))
        await asyncio.sleep(0.05)
        app_dock.close("s1")
        return await asyncio.wait_for(task, 1.0), sub
    out, _ = run(go())
    assert out["error"]["code"] == "closed"


# ---------------------------------------------------------------------------
# apps and open
# ---------------------------------------------------------------------------
def test_find_app_by_id_or_name():
    assert app_dock.find_app("darwin", APPS)["id"] == "darwin"
    assert app_dock.find_app("Darwin's Hoard", APPS)["id"] == "darwin"
    assert app_dock.find_app("links hoard", APPS)["id"] == "links"
    assert app_dock.find_app("LINKS", APPS)["id"] == "links"
    assert app_dock.find_app("nothing", APPS) is None


def test_open_app_starts_a_stopped_app_and_docks_it(monkeypatch):
    started = []
    monkeypatch.setattr(app_dock, "start_through_hub", lambda app_id, timeout=90.0: started.append(app_id) or True)
    async def go():
        return await app_dock.open_app("s1", "links", wait=0)
    out = run(go())
    assert started == ["links"] and out["started"] is True and out["app_id"] == "links" and out["url"] == "http://127.0.0.1:5181"
    assert app_dock.get("s1")["app_id"] == "links"


def test_open_app_errors(monkeypatch):
    with pytest.raises(app_dock.DockError) as e:
        run(app_dock.open_app("s1", "ghost", wait=0))
    assert e.value.code == "app_not_found" and "Darwin" in e.value.message
    monkeypatch.setattr(app_dock, "start_through_hub", lambda app_id, timeout=90.0: False)
    with pytest.raises(app_dock.DockError) as e:
        run(app_dock.open_app("s1", "links", wait=0))
    assert e.value.code == "app_down"
    monkeypatch.setattr(app_dock, "list_apps", lambda: {"apps": [], "available": False, "error": "The Hoard Hub did not answer"})
    with pytest.raises(app_dock.DockError) as e:
        run(app_dock.open_app("s1", "darwin", wait=0))
    assert e.value.code == "hub_unavailable"


def test_open_app_waits_for_the_page_when_a_host_listens():
    async def go():
        sub = app_dock.subscribe("s1")

        async def host():
            ev = await sub.queue.get()
            assert ev["event"] == "open"
            await asyncio.sleep(0.05)
            app_dock.report("s1", ready=True, tools=[{"name": "open_video"}])
        h = asyncio.create_task(host())
        out = await app_dock.open_app("s1", "darwin", wait=2)
        await h
        return out
    out = run(go())
    assert out["ready"] is True and [t["name"] for t in out["tools"]] == ["open_video"]


# ---------------------------------------------------------------------------
# context block and `changed`
# ---------------------------------------------------------------------------
def test_context_block_only_while_docked_and_short():
    assert app_dock.context_block("s1") == ""
    assert app_dock.context_block(None) == ""
    app_dock.dock("s1", "darwin", "Darwin's Hoard", "http://x")
    assert "has not answered" in app_dock.context_block("s1")
    app_dock.report("s1", ready=True, tools=[{"name": "open_video"}, {"name": "tag_item"}],
                    context={"title": "Descubrimientos", "view": "list", "focus": {"kind": "repo", "id": "r1", "label": "faiss"},
                             "selection": "vector search", "summary": "12 nuevos"})
    block = app_dock.context_block("s1")
    for needle in ("Darwin's Hoard (darwin)", "Descubrimientos", "faiss", "vector search", "12 nuevos", "open_video, tag_item",
                   "app_view", "app_act"):
        assert needle in block
    assert len(block) < 1200


def test_note_mcp_call_emits_changed_only_for_the_docked_app():
    async def go():
        sub = app_dock.subscribe("s1")
        assert app_dock.note_mcp_call("s1", "mcp__darwin__add_discovery") is False     # nothing docked yet
        app_dock.dock("s1", "darwin", "Darwin's Hoard", "http://x")
        await sub.queue.get()
        assert app_dock.note_mcp_call("s1", "mcp__darwin__add_discovery") is True
        assert app_dock.note_mcp_call("s1", "mcp__links__add_link") is False
        assert app_dock.note_mcp_call("s1", "bash") is False
        assert app_dock.note_mcp_call("s1", "mcp__darwinshoard__x") is True            # the manifest name also counts
        a, b = await sub.queue.get(), await sub.queue.get()
        return a, b
    a, b = run(go())
    assert a == {"event": "changed", "data": {"server": "darwin", "tool": "add_discovery"}} and b["data"]["server"] == "darwinshoard"


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    import routes.app_dock_routes as ar
    monkeypatch.setattr(ar, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(ar.setup_app_dock_routes())
    return TestClient(app)


def test_routes_apps_open_state_result_close(client):
    r = client.get("/api/app-dock/apps").json()
    assert r["available"] and [a["id"] for a in r["apps"]] == ["darwin", "links"]
    assert client.post("/api/app-dock/open", json={"session": "s1", "app": "ghost"}).status_code == 404
    assert client.post("/api/app-dock/open", json={"app": "darwin"}).status_code == 400
    o = client.post("/api/app-dock/open", json={"session": "s1", "app": "darwin"}).json()
    assert o["app_id"] == "darwin" and o["ready"] is False
    st = client.get("/api/app-dock/state", params={"session": "s1"}).json()
    assert st["app_id"] == "darwin" and st["host"] is False
    posted = client.post("/api/app-dock/state", json={"session": "s1", "ready": True, "tools": [{"name": "t1"}], "context": {"title": "T"}}).json()
    assert posted["ready"] is True and posted["tools"][0]["name"] == "t1" and posted["context"] == {"title": "T"}
    assert client.post("/api/app-dock/state", json={"session": "nope", "ready": True}).status_code == 404
    assert client.post("/api/app-dock/result", json={"session": "s1", "id": "c_zzz", "ok": True}).json() == {"resolved": False}
    assert client.post("/api/app-dock/call", json={"session": "s1", "tool": "app_read"}).json()["error"]["code"] == "no_host"
    assert client.post("/api/app-dock/close", json={"session": "s1"}).json()["closed"] is True
    assert client.get("/api/app-dock/state", params={"session": "s1"}).json()["app_id"] is None


def test_routes_events_stream_delivers_events_and_heartbeats(monkeypatch):
    import routes.app_dock_routes as ar
    monkeypatch.setattr(ar, "require_admin", lambda request: None)
    monkeypatch.setattr(ar, "HEARTBEAT_S", 0.05)
    router = ar.setup_app_dock_routes()
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/app-dock/events")

    class Req:
        def __init__(self):
            self.n = 0

        async def is_disconnected(self):
            self.n += 1
            return self.n > 4

    async def go():
        resp = await endpoint(Req(), session="s1")
        assert resp.media_type == "text/event-stream"
        chunks = []
        first = True
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
            if first and "event: hello" in chunks[-1]:
                first = False
                app_dock.dock("s1", "darwin", "Darwin", "http://x")
        return "".join(chunks)
    text = run(go())
    assert "event: hello" in text and "event: open" in text and '"app": "darwin"' in text and ": keep-alive" in text
    assert app_dock.has_host("s1") is False                              # the stream's end unsubscribed the host


def test_route_result_resolves_a_call_waiting_on_the_host(monkeypatch):
    import routes.app_dock_routes as ar
    monkeypatch.setattr(ar, "require_admin", lambda request: None)

    async def go():
        router = ar.setup_app_dock_routes()
        sub = app_dock.subscribe("s1")
        app_dock.dock("s1", "darwin", "Darwin", "http://x")
        app_dock.report("s1", ready=True)
        await sub.queue.get()
        task = asyncio.create_task(app_dock.call("s1", "app_read", {"scope": "screen"}))
        ev = await sub.queue.get()
        endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/app-dock/result")

        class Req:
            headers = {}
            async def json(self):
                return {"session": "s1", "id": ev["data"]["id"], "ok": True, "result": {"text": "hola"}}
        resp = await endpoint(Req())
        return resp, await task
    resp, out = run(go())
    assert resp == {"resolved": True} and out == {"ok": True, "result": {"text": "hola"}}


# ---------------------------------------------------------------------------
# agent tools
# ---------------------------------------------------------------------------
def _host(session, answer):
    """A fake host: answers every call with `answer(tool, args)`; returns the calls it saw."""
    seen = []

    async def start():
        sub = app_dock.subscribe(session)

        async def loop():
            while True:
                ev = await sub.queue.get()
                if ev["event"] == "open":
                    app_dock.report(session, ready=True, tools=[{"name": "open_video", "description": "Open a video"}],
                                    context={"title": "Videos"})
                if ev["event"] == "call":
                    d = ev["data"]
                    seen.append((d["tool"], d["args"], d["actor"]))
                    ok, payload = answer(d["tool"], d["args"])
                    app_dock.resolve(session, d["id"], ok, payload if ok else None, None if ok else payload)
        return asyncio.create_task(loop())
    return start, seen


def test_tools_open_view_act_end_to_end():
    def answer(tool, args):
        if tool == "app_read":
            return True, "Heading Videos\n[e1] button \"Guardar\""
        if tool == "app_click" and args.get("text") == "Nope":
            return False, {"code": "not_found", "message": "no control Nope", "nearest": ["Guardar"]}
        return True, {"ok": tool, "args": args}
    start, seen = _host("chat-9", answer)

    async def go():
        task = await start()
        o = await app_dock_tools.AppOpenTool().execute({"app": "darwin"}, {"session_id": "chat-9"})
        v = await app_dock_tools.AppViewTool().execute('{"scope": "page"}', {"session_id": "chat-9"})
        a = await app_dock_tools.AppActTool().execute({"action": "click", "target": "Guardar"}, {"session_id": "chat-9"})
        f = await app_dock_tools.AppActTool().execute({"action": "fill", "target": "Buscar", "value": "whisper", "args": {"submit": True}}, {"session_id": "chat-9"})
        own = await app_dock_tools.AppActTool().execute({"action": "open_video", "args": {"id": "3df9"}}, {"session_id": "chat-9"})
        bad = await app_dock_tools.AppActTool().execute({"action": "click", "target": "Nope"}, {"session_id": "chat-9"})
        sc = await app_dock_tools.AppActTool().execute({"action": "scroll", "value": "down"}, {"session_id": "chat-9"})
        hl = await app_dock_tools.AppActTool().execute({"action": "highlight", "target": "Guardar", "value": "aquí"}, {"session_id": "chat-9"})
        task.cancel()
        return o, v, a, f, own, bad, sc, hl
    o, v, a, f, own, bad, sc, hl = run(go())
    assert o["exit_code"] == 0 and "Darwin" in o["output"] and "open_video" in o["output"]
    assert v["exit_code"] == 0 and "Videos" in v["output"] and "[e1]" in v["output"] and "open_video: Open a video" in v["output"]
    assert a["exit_code"] == 0 and f["exit_code"] == 0 and own["exit_code"] == 0
    calls = {c[0]: c for c in seen}
    assert seen[0][:2] == ("app_read", {"scope": "page"})
    assert calls["app_click"][1] in ({"ref": "Guardar", "text": "Guardar"}, {"ref": "Nope", "text": "Nope"})
    assert calls["app_fill"][1] == {"ref": "Buscar", "label": "Buscar", "value": "whisper", "submit": True}
    assert calls["open_video"][1] == {"id": "3df9"}
    assert calls["app_scroll"][1] == {"by": "down"}
    assert calls["app_highlight"][1] == {"ref": "Guardar", "text": "Guardar", "note": "aquí"}
    assert calls["app_click"][2]["name"] == "Faustus" and calls["app_click"][2]["id"] == "chat-9"
    assert bad["exit_code"] == 1 and "not_found" in bad["error_class"] and "Guardar" in bad["error"]


def test_tools_without_app_or_host_fail_fast():
    async def go():
        no_app = await app_dock_tools.AppViewTool().execute({}, {"session_id": "c"})
        act = await app_dock_tools.AppActTool().execute({"action": "click", "target": "x"}, {"session_id": "c"})
        noargs = await app_dock_tools.AppActTool().execute({}, {"session_id": "c"})
        noapp_name = await app_dock_tools.AppOpenTool().execute({}, {"session_id": "c"})
        unknown = await app_dock_tools.AppOpenTool().execute({"app": "ghost"}, {"session_id": "c"})
        await app_dock_tools.AppOpenTool().execute({"app": "darwin"}, {"session_id": "c"})   # nobody listens: docked, not ready
        nohost = await app_dock_tools.AppViewTool().execute({}, {"session_id": "c"})
        return no_app, act, noargs, noapp_name, unknown, nohost
    no_app, act, noargs, noapp_name, unknown, nohost = run(go())
    assert "no_app" in no_app["error_class"] and "no_app" in act["error_class"]
    assert "bad_args" in noargs["error_class"] and "bad_args" in noapp_name["error_class"]
    assert "app_not_found" in unknown["error_class"]
    assert nohost["exit_code"] == 1 and "no_host" in nohost["error_class"] and "app_open" in nohost["error"]


def test_sub_agent_acts_with_its_own_cursor_in_the_chats_dock(monkeypatch):
    runs = {"child-1": SimpleNamespace(name="Researcher", parent_session_id="chat-9")}
    monkeypatch.setattr(app_dock, "_worker_run", lambda sid: runs.get(sid))
    start, seen = _host("chat-9", lambda tool, args: (True, {"done": tool}))

    async def go():
        task = await start()
        await app_dock_tools.AppOpenTool().execute({"app": "darwin"}, {"session_id": "chat-9"})
        out = await app_dock_tools.AppActTool().execute({"action": "wait", "value": "300"}, {"session_id": "child-1"})
        task.cancel()
        return out
    out = run(go())
    assert out["exit_code"] == 0
    tool, args, actor = seen[-1]
    assert tool == "app_wait" and args == {"ms": 300} and actor["name"] == "Researcher" and actor["id"] == "child-1"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_registered_everywhere():
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    for n in NAMES:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in tool_index.BUILTIN_TOOL_DESCRIPTIONS
        assert len(schemas[n]["description"]) < 1000
    from src import tool_security, tool_capabilities
    assert set(NAMES) <= set(tool_security.NON_ADMIN_BLOCKED_TOOLS)
    from src.builtin_mcp import _BUILTIN_SERVERS, NATIVE_TWIN_SERVERS
    assert "app_dock" in _BUILTIN_SERVERS and "app_dock" in NATIVE_TWIN_SERVERS


def test_mcp_server_lists_the_three_tools_with_a_session():
    from mcp_servers import app_dock_server
    tools = run(app_dock_server.list_tools())
    assert [t.name for t in tools] == NAMES
    for t in tools:
        assert "session" in t.inputSchema["properties"] and t.inputSchema["required"][0] == "session"
    bad = run(app_dock_server.call_tool("app_view", {}))
    assert bad.isError is True
