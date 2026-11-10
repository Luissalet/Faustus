"""The Agora proxied for Studio: paths and bodies reach the hub as they are, the request says it comes from the
person's page, and a hub that is down is an answer the page can show, never an HTTP error."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import agora_routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    calls = []

    def fake(method, path, *, query=None, body=None, timeout=30.0, raw=False, with_headers=False):
        calls.append({"method": method, "path": path, "query": query, "body": body, "timeout": timeout, "raw": raw})
        if path.startswith("/api/agora/file/"):
            kind = {"a" * 32: ("image/png", 'inline; filename="foto.png"'), "b" * 32: ("application/octet-stream", 'attachment; filename="datos.bin"')}.get(path.rsplit("/", 1)[1])
            return (200, b"bytes", kind[0], {"Content-Disposition": kind[1]}) if kind else (404, b"", "", {})
        if path == "/api/agora/space_icon":
            return {"atlas": (200, b"\x89PNG-own", "image/png"), "odd": (200, b"<html>", "text/html")}.get(query["space_id"], (404, b"", ""))
        if path == "/api/agora/down":
            return None, None, ""
        if path == "/api/agora/refused":
            return 403, {"ok": False, "error": "only the hub's page may write as 'luis'"}, "application/json"
        if path == "/api/agora/odd":
            return 200, None, "text/html"
        if raw:
            return (200, b"\x89PNG-bytes", "image/png") if "missing" not in path else (404, b"", "")
        return 200, {"ok": True, "echo": path, "version": "1-2"}, "application/json"

    monkeypatch.setattr(agora_routes, "hub_request", fake)
    monkeypatch.setattr(agora_routes, "_icons", {})
    app = FastAPI()
    app.include_router(agora_routes.setup_agora_routes())
    return TestClient(app), calls


def test_reads_and_writes_reach_the_hub_as_they_are(client):
    http, calls = client
    out = http.get("/api/agora/x/guild", params={"space_id": "atlas", "done_limit": "30"}).json()
    assert out == {"ok": True, "echo": "/api/agora/guild", "version": "1-2"}
    assert calls[-1]["method"] == "GET" and calls[-1]["query"] == {"space_id": "atlas", "done_limit": "30"}
    http.get("/api/agora/x/channels/general", params={"space_id": "atlas"})
    assert calls[-1]["path"] == "/api/agora/channels/general"
    http.get("/api/agora/x/console/usage")
    assert calls[-1]["path"] == "/api/agora/console/usage"

    sent = http.post("/api/agora/x/channel_post", json={"space_id": "atlas", "body": "hola", "_person": True, "agent": "luis"})
    assert sent.status_code == 200 and sent.json()["ok"]
    assert calls[-1]["method"] == "POST" and calls[-1]["path"] == "/api/agora/channel_post"
    assert calls[-1]["body"] == {"space_id": "atlas", "body": "hola", "agent": "luis"}   # the page cannot set the hub's own flag


def test_a_hub_that_is_down_or_refuses_is_an_answer_not_an_error(client):
    http, _calls = client
    down = http.get("/api/agora/x/down")
    assert down.status_code == 200 and down.json()["ok"] is False and down.json()["code"] == "hub_unreachable"
    refused = http.post("/api/agora/x/refused", json={})
    assert refused.status_code == 200 and refused.json() == {"ok": False, "error": "only the hub's page may write as 'luis'", "status": 403}
    odd = http.get("/api/agora/x/odd")
    assert odd.status_code == 200 and odd.json()["ok"] is False


@pytest.mark.parametrize("path", ["..%2Fapps", "Guild", "x y", "a" * 200])
def test_paths_outside_the_agora_are_not_forwarded(client, path):
    http, calls = client
    assert http.get("/api/agora/x/" + path).status_code == 404
    assert http.post("/api/agora/x/" + path, json={}).status_code == 404
    assert not calls
    assert http.post("/api/agora/x/post", content=b"[1, 2]", headers={"Content-Type": "application/json"}).status_code == 400


def test_pulse_waits_at_most_twenty_seconds_and_icons_are_cached(client):
    http, calls = client
    assert http.get("/api/agora/pulse", params={"since": "1-1", "wait_s": "300"}).json()["version"] == "1-2"
    assert calls[-1]["query"] == {"since": "1-1", "wait_s": "20.0"} and calls[-1]["timeout"] == 28.0
    first = http.get("/api/agora/icon/atlas")
    assert first.status_code == 200 and first.content == b"\x89PNG-bytes" and first.headers["content-type"] == "image/png"
    assert calls[-1]["path"] == "/api/apps/atlas/icon" and calls[-1]["raw"]
    before = len(calls)
    assert http.get("/api/agora/icon/atlas").status_code == 200 and len(calls) == before
    http.get("/api/agora/icon/hub")
    assert calls[-1]["path"] == "/ui/hub-icon.png"
    assert http.get("/api/agora/icon/missing").status_code == 404
    assert http.get("/api/agora/icon/..%2Fx").status_code == 404


def test_big_icons_are_made_small_and_odd_ones_are_left_alone():
    from io import BytesIO

    Image = pytest.importorskip("PIL.Image")
    import os
    big = BytesIO()
    Image.frombytes("RGB", (1024, 1024), os.urandom(1024 * 1024 * 3)).save(big, format="PNG")   # noise does not compress
    blob, ctype = agora_routes.small_icon(big.getvalue(), "image/png")
    assert ctype == "image/png" and len(blob) < len(big.getvalue()) / 10
    with Image.open(BytesIO(blob)) as small:
        assert max(small.size) == 128
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>" + b" " * 30_000
    assert agora_routes.small_icon(svg, "image/svg+xml") == (svg, "image/svg+xml")
    assert agora_routes.small_icon(b"tiny", "image/png") == (b"tiny", "image/png")
    junk = b"not an image" * 4000
    assert agora_routes.small_icon(junk, "image/png") == (junk, "image/png")


def test_the_request_to_the_hub_carries_the_token_and_the_persons_page(monkeypatch, tmp_path):
    seen = {}

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.update(method=self.command, path=self.path, person=self.headers.get("X-Hoard-Person"),
                        auth=self.headers.get("Authorization"), body=self.rfile.read(length) if length else b"")
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = tmp_path / "mcp-token"
        token.write_text("secret-token\n", encoding="utf-8")
        monkeypatch.delenv(agora_routes.hub_link.ENV, raising=False)
        monkeypatch.setattr(agora_routes.hub_link.hoard_hub, "hub_location",
                            lambda: {"url": f"http://127.0.0.1:{server.server_address[1]}", "token_file": str(token), "configured": True})
        status, payload, _ = agora_routes.hub_request("POST", "/api/agora/channel_post", body={"body": "hola"})
        assert status == 200 and payload == {"ok": True}
        assert seen["person"] == "faustus-studio" and seen["auth"] == "Bearer secret-token"
        assert json.loads(seen["body"]) == {"body": "hola"} and seen["path"] == "/api/agora/channel_post"
        status, _payload, _ = agora_routes.hub_request("GET", "/api/agora/guild", query={"space_id": "a b"})
        assert status == 200 and seen["path"] == "/api/agora/guild?space_id=a+b" and seen["method"] == "GET"
    finally:
        server.shutdown()
        server.server_close()
    monkeypatch.delenv(agora_routes.hub_link.ENV, raising=False)
    monkeypatch.setattr(agora_routes.hub_link.hoard_hub, "hub_location", lambda: {"url": "http://127.0.0.1:9", "token_file": "", "configured": False})
    assert agora_routes.hub_request("GET", "/api/agora/guild", timeout=2.0) == (None, None, "")


def test_a_file_comes_as_the_hub_gives_it_and_never_as_a_page_of_this_app(client):
    http, calls = client
    picture = http.get("/api/agora/file/" + "a" * 32)
    assert picture.status_code == 200 and picture.content == b"bytes" and picture.headers["content-type"] == "image/png"
    assert picture.headers["content-disposition"] == 'inline; filename="foto.png"'
    assert picture.headers["x-content-type-options"] == "nosniff"
    other = http.get("/api/agora/file/" + "b" * 32)
    assert other.headers["content-disposition"] == 'attachment; filename="datos.bin"'
    assert calls[-1]["path"] == "/api/agora/file/" + "b" * 32 and calls[-1]["raw"] is True
    assert http.get("/api/agora/file/" + "c" * 32).status_code == 404
    before = len(calls)
    for bad in ("abc", "A" * 32, "a" * 31 + "/", "..%2F" + "a" * 27):
        assert http.get("/api/agora/file/" + bad).status_code == 404
    assert len(calls) == before                                # only an id is ever asked of the hub


def test_a_servers_own_picture_is_served_only_when_it_is_a_picture(client):
    http, calls = client
    own = http.get("/api/agora/space-icon/atlas")
    assert own.status_code == 200 and own.content == b"\x89PNG-own" and own.headers["content-type"] == "image/png"
    assert calls[-1]["path"] == "/api/agora/space_icon" and calls[-1]["query"] == {"space_id": "atlas"}
    assert http.get("/api/agora/space-icon/odd").status_code == 404        # the hub answered something that is not an image
    assert http.get("/api/agora/space-icon/none").status_code == 404
    assert http.get("/api/agora/space-icon/Not%20A%20Space").status_code == 404


def test_a_file_on_its_way_to_the_hub_is_given_time(client):
    http, calls = client
    http.post("/api/agora/x/file_add", json={"space_id": "atlas", "name": "a.txt", "content_base64": "aG9sYQ=="})
    assert calls[-1]["path"] == "/api/agora/file_add" and calls[-1]["timeout"] == 180.0
    http.post("/api/agora/x/react", json={"space_id": "atlas", "message_id": 3, "emoji": "👍"})
    assert calls[-1]["timeout"] == 40.0
