"""User panels (src/panels.py): the store with its versions, the page and storage routes, the agent
tools, the MCP server, the headless check, and the response headers that let Studio frame a panel."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import panels, tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

PAGE = "<!doctype html><html><head><title>Uso</title></head><body><h1>Hola</h1><script>document.title='ok'</script></body></html>"
NAMES = ["panel_create", "panel_update", "panel_get", "panel_list", "panel_delete", "panel_check", "panel_versions", "panel_restore"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def test_create_get_list_and_ids():
    a = panels.create("Uso diario", PAGE, icon="bar-chart-3", description="stats")
    assert a["id"] == "uso-diario" and a["version"] == 1 and a["order"] == 0
    b = panels.create("Uso diario", PAGE)
    assert b["id"] == "uso-diario-2" and b["order"] == 1 and b["icon"] == "layout-dashboard"
    assert panels.get("uso-diario")["html"] == PAGE
    assert [p["id"] for p in panels.list_panels()] == ["uso-diario", "uso-diario-2"]
    with pytest.raises(panels.PanelError, match="already exists"):
        panels.create("x", PAGE, panel_id="uso-diario")
    with pytest.raises(panels.PanelError):
        panels.get("../etc")
    with pytest.raises(panels.PanelError, match="no such panel"):
        panels.get("nope")
    with pytest.raises(panels.PanelError, match="`html` is required"):
        panels.create("x", "  ")
    assert panels.create("Ñandú ¿?", PAGE)["id"] == "nandu"
    assert panels.create("???", PAGE)["id"] == "panel"


def test_versions_and_restore():
    panels.create("P", PAGE)
    assert panels.update("p", html=PAGE)["html_changed"] is False          # same page: no new version
    v2 = panels.update("p", html=PAGE.replace("Hola", "Adios"))
    assert v2["version"] == 2 and v2["html_changed"] is True
    panels.update("p", html=PAGE.replace("Hola", "Tres"))
    vs = panels.versions("p")
    assert vs["current"] == 3 and [v["version"] for v in vs["versions"]] == [3, 2, 1]
    assert "Hola" in panels.version_html("p", 1)
    r = panels.restore("p", 1)
    assert r["restored_from"] == 1 and r["version"] == 4 and "Hola" in panels.get("p")["html"]
    assert "Tres" in panels.version_html("p", 3)                             # nothing was lost
    with pytest.raises(panels.PanelError, match="no version 9"):
        panels.restore("p", 9)


def test_rename_icon_reorder_delete():
    panels.create("A", PAGE)
    panels.create("B", PAGE)
    panels.create("C", PAGE)
    panels.update("a", name="Alfa", icon="Box!", description="d")
    assert panels.get("a", with_html=False)["name"] == "Alfa" and panels.get("a", with_html=False)["icon"] == "layout-dashboard"
    panels.update("a", icon="box")
    assert panels.get("a", with_html=False)["icon"] == "box"
    assert [p["id"] for p in panels.reorder(["c", "a"])] == ["c", "a", "b"]
    panels.delete("b")
    assert [p["id"] for p in panels.list_panels()] == ["c", "a"]
    with pytest.raises(panels.PanelError):
        panels.delete("b")


def test_page_gets_the_helper_unless_it_has_it():
    panels.create("P", PAGE)
    served = panels.page_html("p")
    assert served.count("faustus-panel.js") == 1 and served.index("faustus-panel.js") < served.index("<h1>")
    own = '<html><head><script src="/api/panels/faustus-panel.js"></script></head><body>x</body></html>'
    panels.create("Q", own)
    assert panels.page_html("q").count("faustus-panel.js") == 1
    panels.create("R", "<p>bare fragment</p>")
    assert panels.page_html("r").startswith(panels.HELPER_TAG)


def test_storage_is_per_panel_and_validated():
    panels.create("P", PAGE)
    panels.create("Q", PAGE)
    panels.storage_set("p", "prefs", {"theme": "x", "n": [1, 2]})
    assert panels.storage_get("p", "prefs") == {"theme": "x", "n": [1, 2]}
    assert panels.storage_get("q", "prefs") is None and panels.storage_list("p") == {"prefs": {"theme": "x", "n": [1, 2]}}
    assert panels.storage_delete("p", "prefs")["deleted"] is True and panels.storage_list("p") == {}
    with pytest.raises(panels.PanelError, match="invalid storage key"):
        panels.storage_set("p", "bad key/..", 1)
    with pytest.raises(panels.PanelError, match="limited"):
        panels.storage_set("p", "big", "x" * (panels.MAX_STORAGE_BYTES + 10))


@pytest.fixture()
def client(monkeypatch):
    import routes.panel_routes as pr
    monkeypatch.setattr(pr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(pr.setup_panel_routes())
    return TestClient(app)


def test_http_routes(client):
    r = client.post("/api/panels", json={"name": "Uso", "html": PAGE, "icon": "bar-chart-3"})
    assert r.status_code == 200 and r.json()["id"] == "uso"
    assert client.post("/api/panels", json={"name": "Uso", "html": PAGE, "id": "uso"}).status_code == 409
    assert client.post("/api/panels", json={"name": "x"}).status_code == 400
    assert client.get("/api/panels").json()["panels"][0]["name"] == "Uso"
    assert client.get("/api/panels/uso").json()["html"] == PAGE
    page = client.get("/api/panels/uso/page")
    assert page.status_code == 200 and "faustus-panel.js" in page.text and page.headers["content-type"].startswith("text/html")
    js = client.get("/api/panels/faustus-panel.js")
    assert js.status_code == 200 and "faustus.storage" in js.text.replace("api.storage", "faustus.storage") and "javascript" in js.headers["content-type"]
    assert client.put("/api/panels/uso", json={"html": PAGE.replace("Hola", "X"), "name": "Uso 2"}).json()["version"] == 2
    assert [v["version"] for v in client.get("/api/panels/uso/versions").json()["versions"]] == [2, 1]
    assert "Hola" in client.get("/api/panels/uso/versions/1").text
    assert client.post("/api/panels/uso/restore", json={"version": 1}).json()["version"] == 3
    assert client.put("/api/panels/uso/storage/k", json={"value": {"a": 1}}).json()["stored"] is True
    assert client.get("/api/panels/uso/storage/k").json()["value"] == {"a": 1}
    assert client.get("/api/panels/uso/storage").json() == {"k": {"a": 1}}
    assert client.delete("/api/panels/uso/storage/k").json()["deleted"] is True
    client.post("/api/panels", json={"name": "Dos", "html": PAGE})
    assert [p["id"] for p in client.post("/api/panels/reorder", json={"ids": ["dos", "uso"]}).json()["panels"]] == ["dos", "uso"]
    assert client.get("/api/panels/uso/check.png").status_code == 404
    assert client.get("/api/panels/nope").status_code == 404
    assert client.delete("/api/panels/uso").json()["deleted"] == "uso"
    assert client.get("/api/panels/uso/page").status_code == 404


def test_panel_tool_route_runs_any_agent_tool(client):
    client.post("/api/panels", json={"name": "Uso", "html": PAGE})
    ok = client.post("/api/panels/tool", json={"tool": "panel_list", "args": {}})
    assert ok.status_code == 200 and "uso" in ok.json()["output"]
    # Tools that run code reach their handler too (here it rejects the empty arguments itself).
    runs = client.post("/api/panels/tool", json={"tool": "loop_card_create", "args": {}})
    assert runs.status_code == 400 and runs.json()["error_class"] != "panel.tool_needs_approval"
    assert client.post("/api/panels/tool", json={"tool": "no_such_tool"}).status_code == 404
    assert client.post("/api/panels/tool", json={}).status_code == 400
    bad = client.post("/api/panels/tool", json={"tool": "panel_get", "args": {}})
    assert bad.status_code == 400


def test_the_page_headers_let_studio_frame_it_and_run_inline_script():
    from core.middleware import SecurityHeadersMiddleware
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/panels/x/page")
    def _page():
        return {"ok": True}

    @app.get("/api/other")
    def _other():
        return {"ok": True}
    c = TestClient(app)
    h = c.get("/api/panels/x/page").headers
    assert h["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in h["content-security-policy"]
    assert "'unsafe-inline'" in h["content-security-policy"].split("script-src")[1].split(";")[0]
    assert "connect-src 'self'" in h["content-security-policy"]
    assert c.get("/api/other").headers["x-frame-options"] == "DENY"


def test_tools_are_registered_everywhere_the_catalogue_looks():
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    from src.tool_index_examples import EXAMPLES
    from src.tool_capabilities import TOOL_CAPABILITIES
    for n in NAMES:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas
        assert n in tool_index.BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES[n] and TOOL_CAPABILITIES[n].effects


def _run(name, args):
    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args), {"owner": "alice"}))


def test_agent_tools_round_trip():
    out = _run("panel_create", {"name": "Uso", "html": PAGE, "icon": "bar-chart-3"})
    assert out["exit_code"] == 0 and out["result"]["id"] == "uso" and "sidebar" in out["output"]
    assert "uso" in _run("panel_list", {})["output"]
    assert "Hola" in _run("panel_get", {"id": "uso"})["output"]
    upd = _run("panel_update", {"id": "uso", "html": PAGE.replace("Hola", "Chau"), "name": "Uso 2"})
    assert upd["exit_code"] == 0 and "html changed" in upd["output"]
    assert "v2" in _run("panel_versions", {"id": "uso"})["output"]
    assert "restored v1" in _run("panel_restore", {"id": "uso", "version": 1})["output"]
    assert _run("panel_get", {"id": "nope"})["exit_code"] == 1
    assert _run("panel_create", {"name": "x"})["exit_code"] == 1
    assert _run("panel_delete", {"id": "uso"})["exit_code"] == 0


def test_mcp_server_lists_and_calls(monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.panels_server as srv
    assert [t.name for t in asyncio.run(srv.list_tools())] == NAMES
    res = asyncio.run(srv.call_tool("panel_create", {"name": "Via MCP", "html": PAGE}))
    assert json.loads(res[0].text)["id"] == "via-mcp"
    assert "Error" in asyncio.run(srv.call_tool("panel_get", {"id": "nope"}))[0].text
    assert "Unknown tool" in asyncio.run(srv.call_tool("other", {}))[0].text
    from src import builtin_mcp
    assert "panels" in builtin_mcp._BUILTIN_SERVERS and "panels" in builtin_mcp.NATIVE_TWIN_SERVERS


class _Handler(BaseHTTPRequestHandler):
    pages = {}

    def do_GET(self):  # noqa: N802
        body = self.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript" if self.path.endswith(".js") else "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):  # noqa: D401
        pass


def test_check_reports_errors_and_a_clean_page_headless():
    pytest.importorskip("playwright")
    panels.create("Bueno", PAGE, panel_id="bueno")
    panels.create("Malo", "<html><body><p>texto</p><script>console.error('boom'); fetch('/api/missing'); undefinedFn();</script></body></html>",
                  panel_id="malo")
    _Handler.pages = {"/api/panels/bueno/page": panels.page_html("bueno"), "/api/panels/malo/page": panels.page_html("malo"),
                      "/api/panels/faustus-panel.js": panels.PANEL_HELPER_JS}
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        try:
            good = panels.check("bueno", base_url=base, wait_ms=100)
        except panels.PanelError as exc:
            if exc.error_class in ("panel.no_browser", "panel.load_failed"):
                pytest.skip(f"no headless browser here: {exc.detail}")
            raise
        assert good["ok"] is True, good
        assert good["title"] == "ok" and good["visible_text_chars"] > 0 and good["screenshot"].endswith("check.png")
        bad = panels.check("malo", base_url=base, wait_ms=100, screenshot=False)
        assert bad["ok"] is False
        assert any("boom" in e["text"] for e in bad["console_errors"])
        assert any("undefinedFn" in e for e in bad["page_errors"])
        assert any(f.get("status") == 404 for f in bad["failed_requests"])
    finally:
        srv.shutdown()


def test_the_helper_exposes_faustus_api_as_documented():
    # Panels call faustus.api(path); window.faustus is the api function itself, so it must also carry .api.
    assert "api.api = api;" in panels.PANEL_HELPER_JS
    assert "window.faustus = api;" in panels.PANEL_HELPER_JS


def test_check_sends_the_internal_token_only_to_the_panel_origin():
    pytest.importorskip("playwright")
    from core.middleware import INTERNAL_TOOL_HEADER
    seen = {"own": [], "other": []}

    def handler_for(kind, pages):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen[kind].append(self.headers.get(INTERNAL_TOOL_HEADER))
                body = pages.get(self.path, "")
                self.send_response(200 if body or kind == "other" else 404)
                self.send_header("Content-Type", "image/gif" if kind == "other" else "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8") if body else b"GIF89a")

            def log_message(self, *a):  # noqa: D401
                pass
        return H

    other = HTTPServer(("127.0.0.1", 0), handler_for("other", {}))
    threading.Thread(target=other.serve_forever, daemon=True).start()
    html = f"<html><body><img src='http://127.0.0.1:{other.server_port}/pixel.gif'><p>hola</p></body></html>"
    panels.create("Fuera", html, panel_id="fuera")
    own = HTTPServer(("127.0.0.1", 0), handler_for("own", {"/api/panels/fuera/page": panels.page_html("fuera")}))
    threading.Thread(target=own.serve_forever, daemon=True).start()
    try:
        try:
            panels.check("fuera", base_url=f"http://127.0.0.1:{own.server_port}", wait_ms=300, screenshot=False, token="secret-token")
        except panels.PanelError as exc:
            if exc.error_class in ("panel.no_browser", "panel.load_failed"):
                pytest.skip(f"no headless browser here: {exc.detail}")
            raise
        assert seen["own"] and all(v == "secret-token" for v in seen["own"])
        assert seen["other"] and all(v is None for v in seen["other"])
    finally:
        own.shutdown()
        other.shutdown()
