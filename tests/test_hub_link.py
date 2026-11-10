"""The hub reached from another machine: the link file a Spark is given, and what goes through it (the Agora and
coordination) and what does not (everything else keeps the usual way to the hub)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src import hub_link


class _Hub(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).seen.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                                "agent": self.headers.get("User-Agent")})
        if self.path == "/api/refuse":
            out, code = {"ok": False, "error": "no"}, 403
        elif self.path == "/api/agent/call":
            out, code = {"ok": True, "tool": body.get("tool"), "result": {"ok": True, "spaces": [{"id": "atlas"}]}}, 200
        else:
            out, code = {"ok": True, "echo": body}, 200
        blob = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


@pytest.fixture
def linked(tmp_path, monkeypatch):
    _Hub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "hub-token"
    token.write_text("secret-1\n", encoding="utf-8")
    link = tmp_path / "hub-link.json"
    link.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "token_file": str(token), "hub": "pc"}), encoding="utf-8")
    monkeypatch.setenv(hub_link.ENV, str(link))
    yield {"url": f"http://127.0.0.1:{server.server_port}", "file": link, "token": token}
    server.shutdown()
    server.server_close()


def test_without_a_link_the_hub_is_found_the_usual_way(monkeypatch):
    monkeypatch.delenv(hub_link.ENV, raising=False)
    monkeypatch.setattr(hub_link.hoard_hub, "hub_location", lambda: {"url": "http://127.0.0.1:8810", "token_file": "", "configured": False})
    assert hub_link.link() is None
    assert hub_link.location() == {"url": "http://127.0.0.1:8810", "token_file": "", "remote": False}
    assert hub_link.post("/api/x", {}) == (None, None)


def test_the_link_file_says_where_the_hub_answers(linked):
    where = hub_link.location()
    assert where["remote"] is True and where["url"] == linked["url"] and hub_link.token(where) == "secret-1"
    status, out = hub_link.post("/api/coordination/repository", {"operation": "list"})
    assert status == 200 and out == {"ok": True, "echo": {"operation": "list"}}
    assert _Hub.seen[-1]["auth"] == "Bearer secret-1" and _Hub.seen[-1]["path"] == "/api/coordination/repository"
    status, out = hub_link.post("/api/refuse", {})
    assert status == 403 and out == {"ok": False, "error": "no"}


def test_a_broken_or_foreign_link_file_is_no_link(tmp_path, monkeypatch):
    path = tmp_path / "hub-link.json"
    monkeypatch.setenv(hub_link.ENV, str(path))
    assert hub_link.link() is None                                  # missing
    path.write_text("not json", encoding="utf-8")
    assert hub_link.link() is None
    path.write_text(json.dumps({"url": "http://example.com:8811", "token_file": "/x"}), encoding="utf-8")
    assert hub_link.link() is None                                  # only this machine's own loopback
    for foreign in ("http://localhost:1@evil.example/", "http://127.0.0.1.evil.example:8811", "https://127.0.0.1:8811",
                    "http://127.0.0.1:8811/elsewhere", "http://127.0.0.1"):
        path.write_text(json.dumps({"url": foreign, "token_file": "/x"}), encoding="utf-8")
        assert hub_link.link() is None, foreign
    path.write_text(json.dumps({"url": "http://127.0.0.1:8811/"}), encoding="utf-8")
    assert hub_link.link() == {"url": "http://127.0.0.1:8811", "token_file": ""}


def test_a_hub_that_is_off_is_nothing_answered(tmp_path, monkeypatch):
    path = tmp_path / "hub-link.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:9", "token_file": ""}), encoding="utf-8")
    monkeypatch.setenv(hub_link.ENV, str(path))
    assert hub_link.post("/api/agent/call", {"tool": "hub_agora_spaces"}, timeout=2) == (None, None)


def test_coordination_goes_through_the_link_and_only_then(linked, monkeypatch):
    from src import shared_coordination as sc

    monkeypatch.setattr("src.family_services.configure", lambda: {})
    monkeypatch.setattr(sc, "_pending", False)
    usual = []
    monkeypatch.setattr(sc.family, "_post", lambda path, body, timeout: (usual.append(path), (200, {"ok": True, "via": "family"}))[1])
    monkeypatch.setattr(sc.family, "call", lambda app, tool, args, timeout=30: (usual.append(tool), {"ok": True, "result": {"ok": True, "via": "family"}})[1])

    out = sc._transport("/api/coordination/repository", {"operation": "list", "args": []})
    assert out["ok"] and out["status"] == 200 and out["echo"]["operation"] == "list"
    assert sc.call("hub_agora_spaces") == {"ok": True, "spaces": [{"id": "atlas"}]}
    assert _Hub.seen[-1]["path"] == "/api/agent/call" and _Hub.seen[-1]["body"]["tool"] == "hub_agora_spaces"
    assert usual == []                                               # nothing went the usual way

    monkeypatch.delenv(hub_link.ENV)
    assert sc._transport("/api/coordination/repository", {"operation": "list"})["via"] == "family"
    assert sc.call("hub_agora_spaces") == {"ok": True, "via": "family"}
    assert usual == ["/api/coordination/repository", "hub_agora_spaces"]


def test_the_agora_routes_use_the_link_and_say_so(linked, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import agora_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(agora_routes.setup_agora_routes())
    http = TestClient(app)
    assert http.get("/api/agora/where").json() == {"ok": True, "remote": True}
    sent = http.post("/api/agora/x/channel_post", json={"space_id": "atlas", "body": "hola"}).json()
    assert sent["ok"] and sent["echo"] == {"space_id": "atlas", "body": "hola"}
    assert _Hub.seen[-1]["path"] == "/api/agora/channel_post" and _Hub.seen[-1]["auth"] == "Bearer secret-1"
    monkeypatch.delenv(hub_link.ENV)
    monkeypatch.setattr(hub_link.hoard_hub, "hub_location", lambda: {"url": "http://127.0.0.1:9", "token_file": "", "configured": False})
    assert http.get("/api/agora/where").json() == {"ok": True, "remote": False}
