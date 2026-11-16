"""Annotations and guided review (src/annotations.py, src/annotation_proxy.py): the store, the composed message,
the diff parser, a real git review, the guide's parsing, the routes, the agent tools, the local-page proxy
(HTML injection, hot-reload socket piping) and every registration point."""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import subprocess
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import annotation_proxy, annotations as A, surfaces

NAMES = ["annotate_open", "annotations_list", "review_open", "review_guide"]
PLAN = {"kind": "plan", "ref": "m1", "title": "Plan de migración"}


@pytest.fixture(autouse=True)
def clean(tmp_path):
    A.set_root_for_tests(str(tmp_path / "annotations"))
    surfaces.reset_for_tests()
    yield
    A.set_root_for_tests(None)
    surfaces.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


# -- store and message ------------------------------------------------------------------
def test_add_list_update_delete_and_persist():
    a = A.add("s1", PLAN, "comment", "migrar la base", "¿Por qué antes que la API?")
    b = A.add("s1", PLAN, "delete", "paso 4", "")
    c = A.add("s1", {"kind": "file", "ref": "/w/README.md"}, "suggestion", "Instalación", "Cómo se instala")
    assert [n["id"] for n in A.list_("s1")] == [a["id"], b["id"], c["id"]]
    assert [n["id"] for n in A.list_("s1", PLAN)] == [a["id"], b["id"]]
    assert A.list_("s2") == []                                          # per chat
    A.update("s1", a["id"], text="otra", type="question")
    got = A.list_("s1", PLAN)[0]
    assert got["type"] == "question" and got["text"] == "otra"
    assert A.delete("s1", b["id"]) == 1 and len(A.list_("s1")) == 2
    st = A.state("s1")
    assert st["pending"] == 2 and st["target"] is None


def test_add_validates():
    with pytest.raises(A.AnnotationError):
        A.add("s1", {"kind": "nope"}, "comment", "q", "t")
    with pytest.raises(A.AnnotationError):
        A.add("s1", PLAN, "shout", "q", "t")
    with pytest.raises(A.AnnotationError):
        A.add("s1", PLAN, "comment", "q", "")                         # a comment needs its text
    with pytest.raises(A.AnnotationError):
        A.add("", PLAN, "comment", "q", "t")
    assert A.add("s1", PLAN, "delete", "solo esto", "")["type"] == "delete"


def test_compose_quotes_each_note_groups_by_target_and_marks_sent():
    A.add("s1", PLAN, "comment", "migrar la base", "Hazlo después")
    A.add("s1", PLAN, "suggestion", "tres días", "una semana")
    A.add("s1", PLAN, "delete", "paso 4", "")
    A.add("s1", PLAN, "question", "", "¿Quién lo prueba?")
    A.add("s1", {"kind": "diff", "ref": "w@HEAD", "title": "Cambios"}, "comment", "x = 1", "Nombre poco claro",
          anchor={"file": "src/a.py", "line": 12, "side": "new"})
    A.add("s1", {"kind": "web", "ref": "http://localhost:5173/"}, "comment", "Comprar", "Más grande",
          anchor={"selector": "#buy"})
    out = A.compose("s1")
    text = out["text"]
    assert out["count"] == 6
    assert "Anotaciones sobre el plan «Plan de migración»:" in text
    assert "1. Comentario sobre «migrar la base»\n   Hazlo después" in text
    assert "2. Cambio propuesto: «tres días» → «una semana»" in text
    assert "3. Borrar: «paso 4»" in text
    assert "4. Pregunta\n   ¿Quién lo prueba?" in text
    assert "en `src/a.py:12` (línea nueva)" in text
    assert "en el elemento `#buy`" in text and "la web «http://localhost:5173/»" in text
    assert A.state("s1")["pending"] == 0
    assert A.compose("s1")["count"] == 0                               # already sent
    again = A.compose("s1", include_sent=True, mark_sent=False)
    assert again["count"] == 6
    A.update("s1", out["ids"][0], pending=True)
    assert A.compose("s1", mark_sent=False)["count"] == 1
    assert A.delete("s1", target=PLAN, sent_only=True) == 3


def test_web_target_must_be_local():
    assert A.clean_target({"kind": "web", "ref": "5173"})["ref"] == "http://localhost:5173/"
    assert A.clean_target({"kind": "web", "ref": "127.0.0.1:8000/app?x=1"})["ref"] == "http://127.0.0.1:8000/app?x=1"
    with pytest.raises(A.AnnotationError):
        A.clean_target({"kind": "web", "ref": "https://example.com/"})


# -- review -------------------------------------------------------------------------------
DIFF = """diff --git a/src/a.py b/src/a.py
index 1..2 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@ def f():
 a = 1
-b = 2
+b = 3
+c = 4
 d = 5
@@ -10,2 +11,2 @@
 x
-y
+z
diff --git a/new.md b/new.md
new file mode 100644
--- /dev/null
+++ b/new.md
@@ -0,0 +1,2 @@
+# Hola
+mundo
\\ No newline at end of file
"""


def test_parse_diff_numbers_lines():
    files = A.parse_diff(DIFF)
    assert [f["path"] for f in files] == ["src/a.py", "new.md"]
    a, new = files
    assert a["status"] == "modified" and a["added"] == 3 and a["removed"] == 2
    assert [h["id"] for h in a["hunks"]] == ["H1", "H2"] and new["hunks"][0]["id"] == "H3"
    h1 = a["hunks"][0]["lines"]
    assert h1[0] == {"k": "ctx", "old": 1, "new": 1, "text": "a = 1"}
    assert h1[1] == {"k": "del", "old": 2, "new": None, "text": "b = 2"}
    assert h1[2] == {"k": "add", "old": None, "new": 2, "text": "b = 3"}
    assert h1[4] == {"k": "ctx", "old": 3, "new": 4, "text": "d = 5"}
    assert a["hunks"][1]["lines"][2]["new"] == 12
    assert new["status"] == "added" and [ln["text"] for ln in new["hunks"][0]["lines"]] == ["# Hola", "mundo"]


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "a.py").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    (repo / "b.md").write_text("# nuevo\n", encoding="utf-8")
    return repo


def test_open_review_on_a_real_repo_saves_with_the_chat(tmp_path):
    repo = _repo(tmp_path)
    res = A.open_review("s1", str(repo), "HEAD")
    assert {f["path"] for f in res["files"]} == {"a.py", "b.md"}
    assert res["stats"]["added"] == 3 and res["stats"]["removed"] == 1
    st = A.state("s1")
    assert st["target"]["kind"] == "diff" and st["review"]["base"] == "HEAD"
    again = A.current_review("s1")
    assert again["stats"] == res["stats"] and again["guide"] is None
    with pytest.raises(A.AnnotationError):
        A.open_review("s1", str(repo), "--evil")
    with pytest.raises(A.AnnotationError):
        A.open_review("s1", str(tmp_path / "missing"), "HEAD")


def test_parse_guide_keeps_known_hunks_and_collects_the_rest():
    files = A.parse_diff(DIFF)
    raw = 'Aquí va: {"overview": "Cambia b", "chapters": [' \
          '{"kind": "glue", "title": "Docs", "summary": "Añade un md", "hunks": ["H3", "H9"]},' \
          '{"kind": "core", "title": "Valor de b", "summary": "b pasa a 3", "hunks": ["h1"]}]} fin'
    g = A.parse_guide(raw, files)
    assert [c["kind"] for c in g["chapters"]] == ["core", "glue", "glue"]
    assert g["chapters"][0]["hunks"] == ["H1"] and g["chapters"][1]["hunks"] == ["H3"]
    assert g["chapters"][2]["title"] == "Resto" and g["chapters"][2]["hunks"] == ["H2"]
    with pytest.raises(A.AnnotationError):
        A.parse_guide("no json here", files)
    text = A.render_guide(g, files)
    assert "## 1. Núcleo: Valor de b" in text and "H1 src/a.py @@ -1,3 +1,4 @@" in text


def test_guide_calls_the_model_and_saves_the_chapters(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    A.open_review("s1", str(repo), "HEAD")
    seen = {}

    async def fake_llm(**kw):
        seen.update(kw)
        return json.dumps({"overview": "o", "chapters": [{"kind": "core", "title": "t", "summary": "s", "hunks": ["H1"]}]})

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm)
    monkeypatch.setattr(A, "_resolve_model", lambda owner, wanted="": ("http://spark/v1", "glm", {}))
    g = run(A.guide("s1"))
    assert g["model"] == "glm" and g["chapters"][0]["hunks"] == ["H1"]
    assert "core: el núcleo" in seen["messages"][0]["content"] and "### H1 a.py" in seen["messages"][0]["content"]
    assert A.current_review("s1")["guide"]["chapters"][0]["title"] == "t"
    with pytest.raises(A.AnnotationError):
        run(A.guide("s-none"))


# -- routes and tools ---------------------------------------------------------------------
def _client(monkeypatch):
    from routes import annotation_routes as ar
    monkeypatch.setattr(ar, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(ar.setup_annotation_routes())
    return TestClient(app)


def test_routes_roundtrip(monkeypatch, tmp_path):
    c = _client(monkeypatch)
    events = []
    monkeypatch.setattr(surfaces, "emit", lambda session, event, data=None: events.append({"session": session, "event": event, "data": data}) or 1)
    r = c.post("/api/annotations/open", json={"session": "s9", "target": PLAN})
    assert r.status_code == 200 and r.json()["target"]["kind"] == "plan"
    note = c.post("/api/annotations", json={"session": "s9", "target": PLAN, "type": "comment", "quote": "q", "text": "t"}).json()
    assert note["id"].startswith("an_")
    assert c.post("/api/annotations", json={"session": "s9", "target": PLAN, "type": "comment"}).status_code == 400
    st = c.get("/api/annotations", params={"session": "s9"}).json()
    assert st["pending"] == 1 and st["target"]["title"] == "Plan de migración"
    assert c.patch(f"/api/annotations/{note['id']}", json={"session": "s9", "text": "t2"}).json()["text"] == "t2"
    msg = c.post("/api/annotations/compose", json={"session": "s9"}).json()
    assert msg["count"] == 1 and "t2" in msg["text"]
    assert c.get("/api/annotations", params={"session": "s9"}).json()["pending"] == 0
    assert c.delete(f"/api/annotations/{note['id']}", params={"session": "s9"}).json() == {"deleted": 1}
    assert c.delete(f"/api/annotations/{note['id']}", params={"session": "s9"}).status_code == 404
    repo = _repo(tmp_path)
    rv = c.post("/api/annotations/review", json={"session": "s9", "workspace": str(repo)}).json()
    assert rv["stats"]["files"] == 2
    assert c.get("/api/annotations/review", params={"session": "s9"}).json()["stats"]["files"] == 2
    assert c.get("/api/annotations/review", params={"session": "other"}).status_code == 404
    assert c.post("/api/annotations/review/guide", json={"session": "other"}).status_code == 404
    assert [e["data"]["target"]["kind"] for e in events if e["event"] == "open"] == ["plan", "diff"]
    assert {e["session"] for e in events} == {"s9"}


def test_agent_tools(tmp_path):
    from src.agent_tools import TOOL_HANDLERS
    repo = _repo(tmp_path)
    ctx = {"session_id": "s5", "workspace": str(repo)}
    out = run(TOOL_HANDLERS["annotate_open"]({"kind": "file", "ref": "b.md"}, ctx))
    assert out["exit_code"] == 0 and out["result"]["target"]["ref"] == os.path.join(str(repo), "b.md")
    assert "No Studio window" in out["output"]
    out = run(TOOL_HANDLERS["annotations_list"]({}, ctx))
    assert out["output"].startswith("No annotations")
    A.add("s5", {"kind": "plan", "ref": "last"}, "question", "fase 2", "¿Cuánto tarda?")
    out = run(TOOL_HANDLERS["annotations_list"]({"pending_only": True}, ctx))
    assert "1 annotations (1 not sent yet)" in out["output"] and "¿Cuánto tarda?" in out["output"]
    assert A.state("s5")["pending"] == 1                               # reading does not mark them sent
    out = run(TOOL_HANDLERS["review_open"]({}, ctx))
    assert out["exit_code"] == 0 and "- a.py (modified, +2 -1)" in out["output"]
    out = run(TOOL_HANDLERS["review_open"]({"base": "--x"}, ctx))
    assert out["exit_code"] == 1 and out["error_class"] == "annotations.git"
    out = run(TOOL_HANDLERS["annotate_open"]({"kind": "web", "ref": "https://example.com"}, ctx))
    assert out["exit_code"] == 1


def test_registration_points():
    from src import builtin_mcp, tool_capabilities, tool_index, tool_index_examples, tool_schemas, tool_security
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    names = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    for n in NAMES:
        assert n in names and n in TOOL_HANDLERS and n in TOOL_TAGS
        assert n in tool_index.BUILTIN_TOOL_DESCRIPTIONS and n in tool_index_examples.EXAMPLES
        assert tool_capabilities.capabilities_for_tool(n).known
        assert n in tool_security.PLAN_MODE_READONLY_TOOLS
    assert builtin_mcp._BUILTIN_SERVERS["annotations"][0] == "mcp_servers/annotations_server.py"
    assert builtin_mcp.is_native_twin("mcp__annotations__review_open")
    assert not builtin_mcp.is_native_twin("mcp__annotations__annotation_add")


def test_mcp_server_lists_the_tools_with_a_session():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "mcp_servers", "annotations_server.py")
    spec = importlib.util.spec_from_file_location("annotations_server_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = run(mod.list_tools())
    assert {t.name for t in tools} == {*NAMES, "annotation_add", "annotations_compose"}
    assert all("session" in t.inputSchema["required"] for t in tools)


# -- the local-page proxy -------------------------------------------------------------------
class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/go"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/")
            self.end_headers()
            return
        body = (b"<html><head><title>T</title></head><body><h1 id='h'>Hola</h1>host=" + self.headers["Host"].encode() + b"</body></html>"
                if self.path == "/" else b"console.log(1)")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/" else "text/javascript")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _get(port, path, extra=b""):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1:" + str(port).encode() + b"\r\nAccept-Encoding: gzip\r\n" + extra + b"\r\n")
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def test_proxy_injects_the_script_and_keeps_paths():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        p = annotation_proxy.ensure(f"http://127.0.0.1:{port}/")
        assert p["upstream"] == f"http://127.0.0.1:{port}/"
        assert annotation_proxy.ensure(f"http://127.0.0.1:{port}/other")["origin"] == p["origin"]   # one per origin
        lport = int(p["origin"].rsplit(":", 1)[1])
        page = _get(lport, "/")
        head, _, body = page.partition(b"\r\n\r\n")
        assert b"200 OK" in head and b"X-Frame-Options" not in head
        assert b"<script data-faustus-annotate>" in body and body.index(b"data-faustus-annotate") < body.index(b"</head>")
        assert f"host=127.0.0.1:{port}".encode() in body                    # upstream sees its own host
        cl = int([ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0])
        assert cl == len(body)
        js = _get(lport, "/app.js")
        assert js.endswith(b"console.log(1)") and b"faustus" not in js
        redirect = _get(lport, "/go")
        assert f"Location: http://127.0.0.1:{lport}/".encode() in redirect or b"Location:" in redirect
    finally:
        srv.shutdown()


def test_proxy_pipes_upgrades_for_hot_reload():
    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"Upgrade: websocket" in head
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        await writer.drain()
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    loop = asyncio.new_event_loop()
    srv = loop.run_until_complete(asyncio.start_server(upstream, "127.0.0.1", 0))
    port = srv.sockets[0].getsockname()[1]
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        p = annotation_proxy.ensure(f"http://localhost:{port}/")
        lport = int(p["origin"].rsplit(":", 1)[1])
        s = socket.create_connection(("127.0.0.1", lport), timeout=5)
        s.sendall(b"GET /?token=x HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        got = b""
        while b"\r\n\r\n" not in got:
            got += s.recv(1024)
        assert b"101" in got
        s.sendall(b"ping")
        rest = got.partition(b"\r\n\r\n")[2]
        while b"echo:ping" not in rest:
            chunk = s.recv(1024)
            if not chunk:
                break
            rest += chunk
        assert b"echo:ping" in rest
        s.close()
    finally:
        loop.call_soon_threadsafe(srv.close)
        loop.call_soon_threadsafe(loop.stop)


def test_inject_without_head():
    assert annotation_proxy.inject(b"<body class=x><p>a</p></body>").startswith(b"<body class=x><script data-faustus-annotate>")
    assert annotation_proxy.inject(b"plain").startswith(b"<script data-faustus-annotate>")


def test_html_files_and_documents_are_served_with_the_script(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><head></head><body><p>Hola</p></body></html>", encoding="utf-8")
    (site / "app.css").write_text("p{color:red}", encoding="utf-8")
    p = annotation_proxy.serve_file(str(site / "index.html"))
    port = int(p["origin"].rsplit(":", 1)[1])
    path = p["url"].split(str(port), 1)[1]
    page = _get(port, path)
    assert b"200 OK" in page and b"data-faustus-annotate" in page
    css = _get(port, path.rsplit("/", 1)[0] + "/app.css")
    assert css.endswith(b"p{color:red}") and b"text/css" in css
    assert b"404" in _get(port, path.rsplit("/", 1)[0] + "/../../etc/passwd")
    d = annotation_proxy.serve_html("<h1>Doc</h1>")
    doc = _get(port, d["url"].split(str(port), 1)[1])
    assert doc.endswith(b"<h1>Doc</h1>") and b"data-faustus-annotate" in doc
    with pytest.raises(A.AnnotationError):
        annotation_proxy.serve_file(str(site / "missing.html"))
