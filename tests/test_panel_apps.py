"""Panels as apps: each panel's own SQLite database (src/panel_db.py), its server actions with the
task scheduler (src/panel_actions.py), the templates (src/panel_templates), their routes, agent and
MCP tools, and the helper's new faustus.db / faustus.actions / faustus.fetch."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import panel_actions, panel_db, panel_templates, panels, tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

PAGE = "<!doctype html><html><head><title>P</title></head><body><h1>Hola</h1></body></html>"
NEW = ["panel_db_query", "panel_db_exec", "panel_action_set", "panel_action_list", "panel_action_run", "panel_from_template",
       "panel_sources"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield
    _clear_tasks()


def _clear_tasks():
    from core.database import ScheduledTask, SessionLocal
    db = SessionLocal()
    try:
        db.query(ScheduledTask).filter(ScheduledTask.action == "panel_action").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _tasks():
    from core.database import ScheduledTask, SessionLocal
    db = SessionLocal()
    try:
        return {r.id: (r.cron_expression, r.status, json.loads(r.prompt), r.output_target, r.notifications_enabled)
                for r in db.query(ScheduledTask).filter(ScheduledTask.action == "panel_action").all()}
    finally:
        db.close()


# --------------------------------------------------------------------------- database
def test_db_script_query_paging_and_types():
    panels.create("P", PAGE)
    out = panel_db.exec_("p", "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, data, blob BLOB);\n"
                              "CREATE INDEX t_name ON t(name); INSERT INTO t (name) VALUES ('a;b')")
    assert out["changes"] == 1
    many = panel_db.exec_("p", "INSERT INTO t (name, data) VALUES (?, ?)",
                          many=[[f"n{i}", {"i": i}] for i in range(30)])
    assert many["changes"] == 30 and many["statements"] == 30
    one = panel_db.exec_("p", "INSERT INTO t (name, blob, data) VALUES (:name, :b, :d)",
                         {"name": "x", "b": b"\x00\x01", "d": True})
    assert one["last_row_id"] == 32
    q = panel_db.query("p", "SELECT id, name, data FROM t WHERE id > ? ORDER BY id", [1], limit=10)
    assert q["columns"] == ["id", "name", "data"] and q["row_count"] == 10 and q["more"] is True and q["next_offset"] == 10
    assert q["rows"][0] == {"id": 2, "name": "n0", "data": '{"i": 0}'}
    last = panel_db.query("p", "SELECT id FROM t WHERE id > ? ORDER BY id", [1], limit=10, offset=30)
    assert [r["id"] for r in last["rows"]] == [32] and last["more"] is False and last["next_offset"] is None
    blob = panel_db.query("p", "SELECT blob, data FROM t WHERE id = 32")["rows"][0]
    assert blob == {"blob": {"base64": "AAE="}, "data": 1}
    assert panel_db.query("p", "SELECT name FROM t WHERE id = 1")["rows"][0]["name"] == "a;b"
    tabs = panel_db.tables("p")
    assert [t["name"] for t in tabs["tables"]] == ["t"] and tabs["tables"][0]["rows"] == 32 and tabs["bytes"] > 0
    assert [c["name"] for c in tabs["tables"][0]["columns"]] == ["id", "name", "data", "blob"]


def test_db_batch_is_one_transaction_and_errors_are_clear():
    panels.create("P", PAGE)
    panel_db.exec_("p", "CREATE TABLE k (v INTEGER UNIQUE)")
    res = panel_db.exec_("p", batch=[{"sql": "INSERT INTO k VALUES (?)", "params": [1]},
                                     {"sql": "INSERT INTO k VALUES (2) RETURNING v"}])
    assert res["changes"] == 2 and res["results"][1]["rows"] == [{"v": 2}]
    with pytest.raises(panels.PanelError, match="UNIQUE"):
        panel_db.exec_("p", batch=[{"sql": "INSERT INTO k VALUES (3)"}, {"sql": "INSERT INTO k VALUES (1)"}])
    assert [r["v"] for r in panel_db.query("p", "SELECT v FROM k ORDER BY v")["rows"]] == [1, 2]   # 3 was rolled back
    with pytest.raises(panels.PanelError, match="one statement"):
        panel_db.query("p", "SELECT 1; SELECT 2")
    with pytest.raises(panels.PanelError, match="sql"):
        panel_db.exec_("p", "  ")
    with pytest.raises(panels.PanelError, match="no such panel"):
        panel_db.query("nope", "SELECT 1")
    # A trigger body keeps its own semicolons inside a script.
    panel_db.exec_("p", "CREATE TABLE log (m); CREATE TRIGGER tg AFTER INSERT ON k BEGIN INSERT INTO log VALUES ('x'); "
                        "INSERT INTO log VALUES ('y'); END; INSERT INTO k VALUES (9);")
    assert panel_db.query("p", "SELECT count(*) AS n FROM log")["rows"][0]["n"] == 2


def test_db_time_limit_interrupts_a_runaway_query():
    panels.create("P", PAGE)
    with pytest.raises(panels.PanelError) as exc:
        panel_db.query("p", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c",
                       timeout_s=0.5)
    assert exc.value.error_class == "panel.db_timeout"
    assert panel_db.query("p", "SELECT 1 AS one")["rows"] == [{"one": 1}]       # the database still answers


# --------------------------------------------------------------------------- actions
def test_extract_paths():
    data = {"gpu_pool": {"util": 40}, "gpu": [{"index": 0, "util": 5}, {"index": 1, "util": 7}],
            "resident": {"models": [{"model": "a"}, {"model": "b"}]}}
    assert panel_actions.extract("gpu_pool.util", data, data) == 40
    assert panel_actions.extract("gpu.1.util", data, data) == 7
    assert panel_actions.extract("resident.models[].model", data, data) == ["a", "b"]
    assert panel_actions.extract("$root.gpu_pool.util", {"x": 1}, data) == 40
    assert panel_actions.extract("$", 5, data) == 5 and panel_actions.extract(3, data, data) == 3
    assert panel_actions.extract("missing.deep", data, data) is None
    assert len(panel_actions.extract("$now", data, data)) == 19


def test_cron_and_spec_validation():
    assert panel_actions.cron_for(5) == "*/5 * * * *" and panel_actions.cron_for(1) == "* * * * *"
    assert panel_actions.cron_for(60) == "0 * * * *" and panel_actions.cron_for(360) == "0 */6 * * *"
    assert panel_actions.cron_for(1440) == "0 0 * * *" and panel_actions.cron_for(2880) == "0 0 */2 * *"
    with pytest.raises(panels.PanelError):
        panel_actions.clean_spec({"name": "a"})                                   # no source
    with pytest.raises(panels.PanelError):
        panel_actions.clean_spec({"name": "a", "url": "/x", "tool": "y"})       # two sources
    with pytest.raises(panels.PanelError):
        panel_actions.clean_spec({"name": "Bad Name", "url": "/x"})
    with pytest.raises(panels.PanelError):
        panel_actions.clean_spec({"name": "a", "url": "ftp://x"})
    with pytest.raises(panels.PanelError, match="cron"):
        panel_actions.clean_spec({"name": "a", "url": "/x", "cron": "nope"})
    with pytest.raises(panels.PanelError, match="table"):
        panel_actions.clean_spec({"name": "a", "url": "/x", "save": {"table": "bad name"}})
    ok = panel_actions.clean_spec({"name": "a", "url": "/api/x", "every_minutes": 5, "save": {"storage": "k"}})
    assert ok["method"] == "GET" and ok["enabled"] is True and ok["save"] == [{"storage": "k"}]


def _serve(payloads):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = payloads.get(self.path)
            if body is None:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "boom"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


USAGE = {"gpu_pool": {"util": 30, "mem_used": 2048}, "gpu": [{"index": 0, "util": 10}, {"index": 1, "util": 50}],
         "resident": {"models": [{"model": "qwen"}]}}


def test_action_saves_rows_storage_and_sql_and_schedules_a_task():
    srv, base = _serve({"/usage": USAGE})
    try:
        panels.create("GPU", PAGE)
        spec = {"url": f"{base}/usage", "every_minutes": 5, "save": [
            {"table": "muestras", "columns": {"ts": "$now", "util": "gpu_pool.util", "modelos": "resident.models[].model"}, "keep": 2},
            {"table": "gpus", "rows": "gpu", "columns": {"idx": "index", "util": "util", "pool": "$root.gpu_pool.util"}},
            {"storage": "ultima", "value": "gpu_pool"},
            {"storage": "serie", "value": "gpu_pool.util", "append": True, "keep": 2},
            {"sql": "INSERT INTO muestras (util) VALUES (:u)", "columns": {"u": "gpu_pool.mem_used"}},
        ]}
        row = panel_actions.set_action("gpu", spec, name="muestra", owner="alice")["action"]
        assert row["schedule"] == "*/5 * * * *" and row["runs"] == 0 and row["next_run"]
        tid = panel_actions.task_id("gpu", "muestra")
        assert _tasks()[tid] == ("*/5 * * * *", "active", {"panel": "gpu", "action": "muestra"}, "none", False)
        for _ in range(3):
            out = asyncio.run(panel_actions.run_action("gpu", "muestra"))
            assert out["ok"] and out["saved"] == 1 + 2 + 1 + 1 + 1
        m = panel_db.query("gpu", "SELECT util, modelos FROM muestras ORDER BY id")["rows"]
        assert len(m) == 3 and m[-1] == {"util": 2048, "modelos": None}            # keep=2 pruned, then the sql target
        assert panel_db.query("gpu", "SELECT util, modelos FROM muestras WHERE modelos IS NOT NULL")["rows"][0]["modelos"] == '["qwen"]'
        g = panel_db.query("gpu", "SELECT idx, util, pool FROM gpus ORDER BY id")["rows"]
        assert len(g) == 6 and g[1] == {"idx": 1, "util": 50, "pool": 30}
        assert panels.storage_get("gpu", "ultima") == {"util": 30, "mem_used": 2048}
        assert [x["value"] for x in panels.storage_get("gpu", "serie")] == [30, 30]
        listed = panel_actions.list_actions("gpu")["actions"][0]
        assert listed["runs"] == 3 and listed["last_ok"] is True and len(listed["history"]) == 3
        # paused: no task; removed: gone with its state
        panel_actions.set_action("gpu", {**spec, "enabled": False}, name="muestra")
        assert tid not in _tasks()
        panel_actions.set_action("gpu", spec, name="muestra")
        assert tid in _tasks()
        panel_actions.remove_action("gpu", "muestra")
        assert tid not in _tasks() and panel_actions.list_actions("gpu")["actions"] == []
    finally:
        srv.shutdown()


def test_failures_are_recorded_and_the_scheduler_entry_skips_quietly():
    srv, base = _serve({})
    try:
        panels.create("P", PAGE)
        panel_actions.set_action("p", {"url": f"{base}/down", "every_minutes": 10}, name="falla")
        with pytest.raises(panels.PanelError, match="HTTP 500"):
            asyncio.run(panel_actions.run_action("p", "falla"))
        st = panel_actions.list_actions("p")["actions"][0]
        assert st["last_ok"] is False and "boom" in st["last_error"] and st["failures"] == 1
        from src.builtin_actions import BUILTIN_ACTIONS, TaskNoop
        assert BUILTIN_ACTIONS["panel_action"] is panel_actions.scheduled_entry
        with pytest.raises(TaskNoop, match="HTTP 500"):
            asyncio.run(panel_actions.scheduled_entry(owner="", prompt=json.dumps({"panel": "p", "action": "falla"})))
        # The panel is deleted: its tasks go with it.
        tid = panel_actions.task_id("p", "falla")
        assert tid in _tasks()
        panels.delete("p")
        assert tid not in _tasks()
    finally:
        srv.shutdown()


def test_scheduled_entry_runs_the_action():
    srv, base = _serve({"/v": {"n": 7}})
    try:
        panels.create("P", PAGE)
        panel_actions.set_action("p", {"url": f"{base}/v", "save": {"storage": "n", "value": "n"}}, name="leer")
        assert _tasks() == {}                                        # no schedule: on demand only
        msg, ok = asyncio.run(panel_actions.scheduled_entry(prompt=json.dumps({"panel": "p", "action": "leer"})))
        assert ok and "1 saved" in msg and panels.storage_get("p", "n") == 7
        assert panel_actions.list_actions("p")["actions"][0]["history"][-1]["trigger"] == "schedule"
    finally:
        srv.shutdown()


def test_tool_actions_call_agent_tools():
    panels.create("P", PAGE)
    panel_actions.set_action("p", {"tool": "panel_list", "save": {"storage": "lista", "value": "panels[].id"}}, name="lista")
    out = asyncio.run(panel_actions.run_action("p", "lista", owner="alice"))
    assert out["saved"] == 1 and panels.storage_get("p", "lista") == ["p"]


def test_light_actions_skip_the_foreground_wait():
    from src import task_scheduler
    assert "panel_action" in task_scheduler.LIGHT_BACKGROUND_ACTIONS
    assert "panel_action" in task_scheduler.TaskScheduler._SILENT_ACTIONS
    assert "panel_action" not in task_scheduler.TaskScheduler._MODEL_BACKED_ACTIONS


# --------------------------------------------------------------------------- templates
def test_templates_exist_and_have_the_helper():
    ids = [t["id"] for t in panel_templates.list_templates()]
    assert ids == ["gpu", "agora", "darwin", "notas", "uso"]
    for t in ids:
        html = panel_templates.html(t)
        assert "/api/panels/faustus-panel.js" in html and 'lang="es"' in html and "var(--fs-bg)" in html
    with pytest.raises(panels.PanelError, match="no template"):
        panel_templates.html("nope")


def test_create_from_template_sets_up_tables_and_actions(monkeypatch):
    async def fake_call(spec, owner=None):
        return USAGE | {"gpu_pool": {"util": 12, "util_avg": 3, "mem_used": 1024, "mem_total": 8192},
                        "ram": {"percent": 40}, "cpu": {"percent": 5}}
    monkeypatch.setattr(panel_actions, "call_source", fake_call)
    out = asyncio.run(panel_templates.create_from_template("gpu", owner="alice"))
    assert out["id"] == "gpu-y-modelos" and out["actions"] == ["muestra"] and out["first_runs"][0]["ok"]
    tabs = {t["name"]: t for t in panel_db.tables("gpu-y-modelos")["tables"]}
    assert tabs["muestras"]["rows"] == 1 and tabs["gpus"]["rows"] == 2
    row = panel_db.query("gpu-y-modelos", "SELECT util, mem_total, modelos, ram_pct FROM muestras")["rows"][0]
    assert row == {"util": 12, "mem_total": 8192, "modelos": '["qwen"]', "ram_pct": 40}
    assert panel_actions.task_id("gpu-y-modelos", "muestra") in _tasks()
    notas = asyncio.run(panel_templates.create_from_template("notas", name="Mi tablero"))
    assert notas["id"] == "mi-tablero" and notas["actions"] == []
    assert [t["name"] for t in panel_db.tables("mi-tablero")["tables"]] == ["tarjetas"]
    with pytest.raises(panels.PanelError, match="no template"):
        asyncio.run(panel_templates.create_from_template("nope"))


# --------------------------------------------------------------------------- routes, tools, MCP
@pytest.fixture()
def client(monkeypatch):
    import routes.panel_routes as pr
    monkeypatch.setattr(pr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(pr.setup_panel_routes())
    return TestClient(app)


def test_http_routes(client):
    srv, base = _serve({"/v": {"n": 3}})
    try:
        client.post("/api/panels", json={"name": "Uso", "html": PAGE})
        assert client.post("/api/panels/uso/db/exec", json={"sql": "CREATE TABLE a (x)"}).status_code == 200
        assert client.post("/api/panels/uso/db/exec", json={"sql": "INSERT INTO a VALUES (?)", "many": [[1], [2], [3]]}).json()["changes"] == 3
        q = client.post("/api/panels/uso/db/query", json={"sql": "SELECT x FROM a ORDER BY x", "limit": 2}).json()
        assert q["rows"] == [{"x": 1}, {"x": 2}] and q["next_offset"] == 2
        assert client.post("/api/panels/uso/db/exec", json={"batch": [{"sql": "DELETE FROM a WHERE x = ?", "params": [1]}]}).json()["changes"] == 1
        assert client.get("/api/panels/uso/db/tables").json()["tables"][0]["rows"] == 2
        assert client.post("/api/panels/uso/db/query", json={"sql": "SELEC x"}).status_code == 400
        put = client.put("/api/panels/uso/actions/leer", json={"url": f"{base}/v", "save": {"storage": "n", "value": "n"}})
        assert put.status_code == 200 and put.json()["action"]["name"] == "leer"
        assert client.post("/api/panels/uso/actions/leer/run").json()["saved"] == 1
        assert client.get("/api/panels/uso/storage/n").json()["value"] == 3
        assert client.get("/api/panels/uso/actions").json()["actions"][0]["runs"] == 1
        assert client.put("/api/panels/uso/actions/mal", json={"url": f"{base}/no"}).status_code == 200
        assert client.post("/api/panels/uso/actions/mal/run").status_code == 502
        assert client.delete("/api/panels/uso/actions/mal").json()["removed"] == "mal"
        assert client.delete("/api/panels/uso/actions/mal").status_code == 404
        f = client.post("/api/panels/fetch", json={"url": f"{base}/v"}).json()
        assert f["ok"] is True and f["body"] == {"n": 3}
        assert client.post("/api/panels/fetch", json={"url": "file:///etc/passwd"}).status_code == 400
        assert [t["id"] for t in client.get("/api/panels/templates").json()["templates"]][:2] == ["gpu", "agora"]
        made = client.post("/api/panels/from-template", json={"template": "notas", "id": "tablero"}).json()
        assert made["id"] == "tablero" and made["template"] == "notas"
        assert client.post("/api/panels/from-template", json={"template": "nope"}).status_code == 404
    finally:
        srv.shutdown()


@pytest.fixture()
def fake_hub(monkeypatch):
    """A Hub on a local port: hub:/... URLs resolve to it, the way family_services resolves the real one."""
    srv, base = _serve({
        "/api/agora/spaces": {"ok": True, "spaces": [{"id": "faustus", "title": "Faustus", "counts": {"active": 2}}]},
        "/api/apps": {"apps": [{"id": "darwin", "name": "Darwin's Hoard", "purpose": "discoveries", "kind": "app"},
                               {"id": "gone", "name": "Gone", "purpose": "stopped", "kind": "app"}]},
        "/api/apps/darwin/tools": {"tools": [{"name": "darwin_overview", "description": "Overview.\nmore",
                                              "inputSchema": {"properties": {"limit": {"type": "integer"}}}}]},
    })
    real = panel_actions._resolve_url

    def resolve(url):
        if url.startswith("hub:"):
            return base + url[4:], {"Authorization": "Bearer t"}
        return real(url)
    monkeypatch.setattr(panel_actions, "_resolve_url", resolve)
    yield base
    srv.shutdown()


def test_hub_route_and_helper(client, fake_hub):
    r = client.post("/api/panels/hub", json={"path": "/api/agora/spaces"}).json()
    assert r["ok"] is True and r["body"]["spaces"][0]["id"] == "faustus"
    assert client.post("/api/panels/hub", json={"path": "/nope"}).json()["ok"] is False
    assert client.post("/api/panels/hub", json={"path": "http://x"}).status_code == 400
    js = panels.PANEL_HELPER_JS
    assert "api.hub = function" in js and "'/api/panels/hub'" in js


def test_panel_sources_lists_routes_hub_and_hoards(client, fake_hub):
    from src import panel_sources
    d = panel_sources.sources()
    assert any(r["path"] == "/api/system/usage" for r in d["faustus_routes"])
    assert any(r["path"].startswith("/api/agora/tasks") for r in d["hub_routes"])
    apps = {a["id"]: a for a in d["hoards"]["apps"]}
    assert apps["darwin"]["tools"] == ["darwin_overview"] and apps["gone"]["note"] == "not answering now"
    text = panel_sources.render(d)
    assert "faustus.hub" in text and "Ágora" in text and "darwin_overview" in text
    one = panel_sources.sources(app="darwin")
    assert one["tools"][0] == {"name": "darwin_overview", "description": "Overview.", "args": {"limit": "integer"}}
    pr = panel_sources.sources(probe_path="hub:/api/agora/spaces")["probe"]
    assert pr["status"] == 200 and "spaces" in pr["shape"] and "counts" in pr["shape"]
    out = _run("panel_sources", {"app": "darwin"})
    assert out["exit_code"] == 0 and "darwin_overview(limit: integer)" in out["output"]
    assert client.get("/api/panels/sources", params={"probe": "hub:/api/apps"}).json()["probe"]["ok"] is True


def _run(name, args):
    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args), {"owner": "alice"}))


def test_agent_tools_round_trip():
    panels.create("P", PAGE)
    assert "empty" in _run("panel_db_query", {"id": "p"})["output"]
    assert _run("panel_db_exec", {"id": "p", "sql": "CREATE TABLE n (t TEXT); INSERT INTO n VALUES ('hola')"})["exit_code"] == 0
    assert "n (table, 1 rows)" in _run("panel_db_query", {"id": "p"})["output"]
    assert "hola" in _run("panel_db_query", {"id": "p", "sql": "SELECT t FROM n"})["output"]
    assert _run("panel_db_exec", {"id": "p", "sql": "INSERT INTO n VALUES (?)", "many": [["a"], ["b"]]})["result"]["changes"] == 2
    bad = _run("panel_db_query", {"id": "p", "sql": "SELECT * FROM nada"})
    assert bad["exit_code"] == 1 and "no such table" in bad["error"]
    s = _run("panel_action_set", {"id": "p", "name": "lista", "tool": "panel_list", "save": {"storage": "l"}, "run_now": True})
    assert s["exit_code"] == 0 and "first run: ok" in s["output"]
    assert "lista" in _run("panel_action_list", {"id": "p"})["output"]
    assert "ran lista" in _run("panel_action_run", {"id": "p", "name": "lista"})["output"]
    assert "removed" in _run("panel_action_set", {"id": "p", "name": "lista", "remove": True})["output"]
    listing = _run("panel_from_template", {})
    assert "gpu:" in listing["output"] and "notas:" in listing["output"]
    made = _run("panel_from_template", {"template": "notas"})
    assert made["exit_code"] == 0 and "notas-y-tablero" in made["output"]


def test_tools_are_registered_everywhere_the_catalogue_looks():
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index_examples import EXAMPLES
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    for n in NEW:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in NON_ADMIN_BLOCKED_TOOLS
        assert n in tool_index.BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES[n] and TOOL_CAPABILITIES[n].effects


def test_mcp_server_has_the_new_tools():
    pytest.importorskip("mcp")
    import mcp_servers.panels_server as srv
    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert all(n in names for n in NEW)
    panels.create("P", PAGE)
    res = asyncio.run(srv.call_tool("panel_db_exec", {"id": "p", "sql": "CREATE TABLE z (a)"}))
    assert json.loads(res[0].text)["id"] == "p"


def test_the_helper_exposes_db_actions_and_fetch():
    js = panels.PANEL_HELPER_JS
    for needle in ("api.db = {", "'/db/query'", "'/db/exec'", "'/db/tables'", "api.actions = {", "'/run'", "api.fetch = function",
                   "'/api/panels/fetch'"):
        assert needle in js, needle
