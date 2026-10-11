"""GET /api/family/fleet through the REAL AuthMiddleware (AUTH_ENABLED=true, no localhost bypass).

The Hub reads this route with the credential it shares with the family, which is neither an ``ody_`` API token
nor a session cookie. A handler-only check never ran in production: the middleware answered 401 first. The
server here is the real app in a subprocess with a temporary data directory.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HUB_TOKEN = "hub-qa-token-0123456789"
ADMIN_USER, ADMIN_PASSWORD = "fleetadmin", "fleet-admin-password-1"

pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, headers: dict | None = None, method: str = "GET", body: bytes | None = None):
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {}), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw) if raw else {}, e.headers
        except ValueError:
            return e.code, {"raw": raw}, e.headers


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="faustus-fleet-auth-")
    token_file = Path(data_dir) / "hub-token"
    token_file.write_text(HUB_TOKEN, encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "ODYSSEUS_DATA_DIR": data_dir,
        "DATABASE_URL": "sqlite:///" + data_dir.replace("\\", "/") + "/app.db",
        "APP_PORT": str(port),
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "HOARD_HUB_TOKEN_FILE": str(token_file),
        "HOARD_HUB_URL": "http://127.0.0.1:9",
        "ODYSSEUS_INPROCESS_POLLERS": "0",
        "ODYSSEUS_INPROCESS_TASKS": "0",
        "ODYSSEUS_STARTUP_WARMUPS": "0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    log = open(os.path.join(data_dir, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        end = time.time() + 180
        while time.time() < end:
            try:
                if _get(base + "/api/health")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("Faustus did not start")
        creds = json.dumps({"username": ADMIN_USER, "password": ADMIN_PASSWORD}).encode()
        json_header = {"Content-Type": "application/json"}
        assert _get(base + "/api/auth/setup", json_header, "POST", creds)[0] == 200
        status, _, headers = _get(base + "/api/auth/login", json_header, "POST", creds)
        assert status == 200
        cookie = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        assert cookie.startswith("odysseus_session=")
        yield {"base": base, "cookie": cookie}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def test_fleet_route_needs_a_credential(server):
    status, body, _ = _get(server["base"] + "/api/family/fleet")
    assert status == 401


def test_a_wrong_bearer_is_refused_by_the_middleware(server):
    for bad in ("Bearer not-the-hub-token", "Bearer " + HUB_TOKEN + "x", "Bearer ", "Bearer ody_" + "a" * 43):
        status, _, _ = _get(server["base"] + "/api/family/fleet", {"Authorization": bad})
        assert status in (401, 403), bad


def test_the_hub_token_reads_the_published_metadata(server):
    status, body, _ = _get(server["base"] + "/api/family/fleet", {"Authorization": "Bearer " + HUB_TOKEN})
    assert status == 200, body
    assert body["ok"] is True and body["owner"] == "faustus" and isinstance(body["items"], list)


def test_an_administrator_session_still_reads_it(server):
    status, body, _ = _get(server["base"] + "/api/family/fleet", {"Cookie": server["cookie"]})
    assert status == 200 and body["owner"] == "faustus"


def test_the_exemption_is_exactly_one_get_route(server):
    auth = {"Authorization": "Bearer " + HUB_TOKEN}
    for path in ("/api/hoard/fleet", "/api/tasks", "/api/family/fleet/extra", "/api/sessions"):
        status, _, _ = _get(server["base"] + path, auth)
        assert status in (401, 403, 404), (path, status)
    status, _, _ = _get(server["base"] + "/api/family/fleet", auth, "POST", b"{}")
    assert status in (401, 403, 405)
