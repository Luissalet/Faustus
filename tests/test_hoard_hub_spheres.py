"""The family hub's spheres, proxied by Faustus: routes, hub lookup and the
agent's one-line context, against a fake hub on 127.0.0.1."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.hoard_hub_routes import setup_hoard_hub_routes
from src import connector_sidecar, hoard_hub

TOKEN = "hub-secret-token"


class FakeHub:
    def __init__(self):
        self.active = "personal"
        self.token = TOKEN
        self.requests = []
        self.spheres = [
            {"id": "personal", "name": {"es": "Personal", "en": "Personal"}, "color": "#c9a227",
             "vip": ["boss@corp.com"], "mail_accounts": ["*"]},
            {"id": "work", "name": {"es": "Trabajo", "en": "Work"}, "color": "#3d7bd9",
             "vip": [], "mail_accounts": []},
        ]
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                hub.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/api/spheres":
                    self._send(200, {"ok": True, "active": hub.active, "spheres": hub.spheres})
                else:
                    self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                auth = self.headers.get("Authorization")
                hub.requests.append(("POST", self.path, auth, body))
                if auth != "Bearer " + hub.token:
                    return self._send(401, {"ok": False, "error": "a family bearer token is required"})
                if self.path == "/api/spheres/active":
                    if body.get("id") not in {s["id"] for s in hub.spheres}:
                        return self._send(404, {"ok": False, "error": "unknown sphere: " + str(body.get("id"))})
                    hub.active = body["id"]
                    return self._send(200, {"ok": True, "active": hub.active})
                self._send(404, {"ok": False, "error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hub():
    fake = FakeHub()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for var in ("HOARD_HUB_URL", "HOARD_HUB_TOKEN_FILE", "HOARD_HUB_DATA_DIR", "HOARDLINK_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(connector_sidecar, "DATA_DIR", str(tmp_path / "faustus-data"))
    hoard_hub._reset_for_tests()
    yield
    hoard_hub._reset_for_tests()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(setup_hoard_hub_routes())
    with TestClient(app) as c:
        yield c


def token_file(tmp_path, text=TOKEN):
    path = tmp_path / "mcp-token"
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_get_lists_slim_spheres_and_the_hub_url(client, hub, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    body = client.get("/api/hoard/spheres").json()
    assert body["ok"] is True
    assert body["active"] == "personal"
    assert body["hub_url"] == hub.url
    assert [s["id"] for s in body["spheres"]] == ["personal", "work"]
    assert body["spheres"][1] == {"id": "work", "name": {"es": "Trabajo", "en": "Work"}, "color": "#3d7bd9"}
    assert "vip" not in json.dumps(body), "the hub's private rules must not reach the browser"


def test_switch_sends_the_hub_token_and_returns_the_new_state(client, hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", token_file(tmp_path))
    response = client.post("/api/hoard/spheres/active", json={"id": "work"})
    assert response.status_code == 200
    assert response.json()["active"] == "work"
    assert hub.active == "work"
    post = next(r for r in hub.requests if r[0] == "POST")
    assert post[2] == "Bearer " + TOKEN and post[3] == {"id": "work"}


def test_switch_without_a_token_is_reported_as_refused_not_as_an_error(client, hub, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    response = client.post("/api/hoard/spheres/active", json={"id": "work"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "hub refused"}
    assert hub.active == "personal"


def test_hub_down_answers_200_unreachable_for_both_routes(client, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_URL", free_port_url())
    for response in (client.get("/api/hoard/spheres"),
                     client.post("/api/hoard/spheres/active", json={"id": "work"})):
        assert response.status_code == 200
        assert response.json() == {"ok": False, "error": "hub unreachable"}


def test_unknown_sphere_keeps_the_hubs_404_and_an_empty_id_is_a_400(client, hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", token_file(tmp_path))
    missing = client.post("/api/hoard/spheres/active", json={"id": "nope"})
    assert missing.status_code == 404 and missing.json()["ok"] is False
    assert client.post("/api/hoard/spheres/active", json={}).status_code == 400
    assert hub.active == "personal"


def test_the_hoardhub_connector_gives_the_url_and_its_token_file(client, hub, tmp_path):
    hub_dir = tmp_path / "HoardLink"
    (hub_dir / "data").mkdir(parents=True)
    token_file_in_hub = hub_dir / "data" / "mcp-token"
    token_file_in_hub.write_text(TOKEN, encoding="utf-8")
    connector_sidecar.create_connector(
        preset_id="hoardhub", server_id="hoardhub", owner=None,
        values={"HOARDLINK_DIR": str(hub_dir), "TOKEN_FILE": str(token_file_in_hub)},
        app_url=hub.url, ui_url=hub.url,
    )
    where = hoard_hub.hub_location()
    assert where["url"] == hub.url and where["configured"] is True
    assert where["token_file"] == str(token_file_in_hub)
    assert client.post("/api/hoard/spheres/active", json={"id": "work"}).json()["active"] == "work"


def test_the_token_defaults_to_the_hub_folder_next_to_the_connector(tmp_path, hub):
    hub_dir = tmp_path / "HoardLink"
    (hub_dir / "data").mkdir(parents=True)
    (hub_dir / "data" / "mcp-token").write_text(TOKEN, encoding="utf-8")
    connector_sidecar.create_connector(
        preset_id="hoardhub", server_id="hoardhub", owner=None,
        values={"HOARDLINK_DIR": str(hub_dir)}, app_url=hub.url, ui_url=hub.url,
    )
    assert hoard_hub.hub_location()["token_file"] == str(hub_dir / "data" / "mcp-token")


def test_the_url_file_in_the_hub_data_folder_is_used_when_nothing_else_says(tmp_path, hub, monkeypatch):
    data = tmp_path / "hubdata"
    data.mkdir()
    (data / "url").write_text(hub.url + "\n", encoding="utf-8")
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", str(data))
    where = hoard_hub.hub_location()
    assert where["url"] == hub.url and where["configured"] is True
    assert hoard_hub.get_spheres()["ok"] is True


def test_with_nothing_configured_the_default_url_is_tried_and_not_configured():
    where = hoard_hub.hub_location()
    assert where == {"url": "http://127.0.0.1:8810", "token_file": "", "configured": False}


def test_switching_needs_an_admin_when_auth_is_on(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", token_file(tmp_path))
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user == "admin")

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("X-Fixture-User")
        return await call_next(request)

    app.include_router(setup_hoard_hub_routes())
    with TestClient(app) as c:
        assert c.post("/api/hoard/spheres/active", json={"id": "work"}, headers={"X-Fixture-User": "guest"}).status_code == 403
        assert hub.active == "personal"
        ok = c.post("/api/hoard/spheres/active", json={"id": "work"}, headers={"X-Fixture-User": "admin"})
        assert ok.status_code == 200 and hub.active == "work"
        assert c.get("/api/hoard/spheres").status_code == 200


def test_context_line_is_empty_until_the_hub_answers_then_names_the_sphere(hub, monkeypatch):
    assert hoard_hub.context_line() == ""
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    hoard_hub.get_spheres()
    assert hoard_hub.context_line() == "Active sphere (Hoard Hub): Personal"
    hub.active = "work"
    hoard_hub.get_spheres()
    assert hoard_hub.context_line() == "Active sphere (Hoard Hub): Work"


def test_context_line_goes_away_when_the_answer_is_stale_or_the_hub_stops(hub, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    hoard_hub.get_spheres()
    assert hoard_hub.context_line()
    real = time.time
    monkeypatch.setattr(hoard_hub.time, "time", lambda: real() + hoard_hub.CONTEXT_TTL_S + 5)
    monkeypatch.setenv("HOARD_HUB_URL", free_port_url())
    assert hoard_hub.context_line() == ""
    # A failed refresh also clears what was known.
    monkeypatch.setattr(hoard_hub.time, "time", real)
    hoard_hub._reset_for_tests()
    hoard_hub.get_spheres()
    assert hoard_hub.context_line() == ""


def test_context_line_never_blocks_the_turn_and_fills_in_from_a_background_refresh(hub, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    started = time.monotonic()
    assert hoard_hub.context_line() == ""  # configured, nothing known yet: asks for a refresh, returns at once
    assert time.monotonic() - started < 0.5
    deadline = time.monotonic() + 5
    line = ""
    while time.monotonic() < deadline and not line:
        time.sleep(0.05)
        line = hoard_hub.context_line()
    assert line == "Active sphere (Hoard Hub): Personal"


def test_the_date_context_message_carries_the_line_only_when_the_hub_answered(hub, monkeypatch):
    from src.user_time import current_datetime_context_message

    plain = current_datetime_context_message()["content"]
    assert "Active sphere" not in plain
    monkeypatch.setenv("HOARD_HUB_URL", hub.url)
    hub.active = "work"
    hoard_hub.get_spheres()
    with_line = current_datetime_context_message()["content"]
    assert "Active sphere (Hoard Hub): Work" in with_line
    assert with_line.startswith("[Context — current date/time")
    assert with_line.count("## Current date and time") == 1


def test_the_studio_chip_check_passes():
    """studio/checks/sphere-chip.check.mjs: the real chip under happy-dom."""
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if not (shutil.which("node") and (root / "node_modules" / "happy-dom").exists()
            and (root / "node_modules" / "esbuild" / "lib" / "main.js").exists()):
        pytest.skip("node + node_modules (happy-dom, esbuild) needed")
    result = subprocess.run(["node", "studio/checks/sphere-chip.check.mjs"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
