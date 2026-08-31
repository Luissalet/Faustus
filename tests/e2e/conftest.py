"""End-to-end browser tests (Playwright) for the key agent flows.

Opt-in: they start a real Odysseus server (temp data dir, localhost bypass,
no auth) plus a scripted fake model endpoint, and drive the UI in headless
Chromium. Run with:

    ODYSSEUS_E2E=1 python -m pytest tests/e2e -q

Requirements: `pip install playwright` + `playwright install chromium` (or
PLAYWRIGHT_BROWSERS_PATH pointing at an installed Chromium). Skipped otherwise.
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
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
E2E = os.environ.get("ODYSSEUS_E2E", "").strip().lower() in {"1", "true", "yes", "on"}

try:  # pragma: no cover - import guard
    from playwright.sync_api import sync_playwright  # noqa: F401
    _HAS_PW = True
except Exception:  # noqa: BLE001
    _HAS_PW = False


def pytest_collection_modifyitems(config, items):
    if E2E and _HAS_PW:
        return
    reason = "set ODYSSEUS_E2E=1 to run the browser flows" if not E2E else "playwright is not installed"
    skip = pytest.mark.skip(reason=reason)
    here = str(Path(__file__).resolve().parent)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 90.0) -> None:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up: {last}")


def _post_form(url: str, data: dict) -> dict:
    import urllib.parse
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _post_json(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class FakeLLM:
    def __init__(self, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def script(self, responses, delay: float = 0.0) -> None:
        _post_json(self.base + "/_script", {"responses": responses, "delay": delay, "reset": True})

    def calls(self) -> dict:
        with urllib.request.urlopen(self.base + "/_calls", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))


@pytest.fixture(scope="session")
def fake_llm():
    from tests.e2e import fake_llm as mod
    port = _free_port()
    srv = mod.serve(port)
    yield FakeLLM(port)
    srv.shutdown()


class AppServer:
    def __init__(self, base: str, data_dir: str, endpoint_id: str, endpoint_url: str):
        self.base = base
        self.data_dir = data_dir
        self.endpoint_id = endpoint_id
        self.endpoint_url = endpoint_url

    def new_session(self, name: str = "e2e") -> str:
        r = _post_form(self.base + "/api/session", {
            "name": name, "endpoint_id": self.endpoint_id, "endpoint_url": self.endpoint_url,
            "model": "fake-coder", "skip_validation": "true",
        })
        return r.get("id") or r.get("session_id")

    def activity(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/chat/activity", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))


@pytest.fixture(scope="session")
def app_server(fake_llm):
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="odysseus-e2e-data-")
    env = dict(os.environ)
    env.update({
        "ODYSSEUS_DATA_DIR": data_dir,
        "DATABASE_URL": "sqlite:///" + (data_dir.replace("\\", "/") + "/app.db"),
        "APP_PORT": str(port),
        "LOCALHOST_BYPASS": "true",
        "AUTH_ENABLED": "false",
        "ODYSSEUS_INPROCESS_POLLERS": "0",
        "ODYSSEUS_INPROCESS_TASKS": "0",
        "ODYSSEUS_STARTUP_WARMUPS": "0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    log = open(os.path.join(data_dir, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_http(base + "/api/chat/activity", timeout=120)
        ep = _post_form(base + "/api/model-endpoints", {
            "name": "fake", "base_url": fake_llm.base + "/v1", "skip_probe": "true", "endpoint_kind": "local",
        })
        ep_id = ep.get("id") or (ep.get("endpoint") or {}).get("id")
        yield AppServer(base, data_dir, ep_id, fake_llm.base + "/v1")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, app_server):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(20000)
    yield pg
    ctx.close()


@pytest.fixture
def workspace(tmp_path):
    """A tiny pytest project the fake model edits."""
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    return ws


def open_chat(page, app_server: AppServer, session_id: str, workspace: str | None) -> None:
    page.goto(app_server.base + "/", wait_until="domcontentloaded")
    if workspace:
        page.evaluate("ws => localStorage.setItem('odysseus-workspace', ws)", workspace)
    page.goto(app_server.base + "/#" + session_id, wait_until="domcontentloaded")
    page.wait_for_selector("#message:visible")
    # A previous session may still be streaming into the composer state;
    # give the app a beat to select the hash session.
    page.wait_for_timeout(500)


def send_agent_message(page, text: str) -> None:
    agent_btn = page.locator("#mode-agent-btn")
    if agent_btn.count() and "active" not in (agent_btn.get_attribute("class") or ""):
        agent_btn.click()
    # The welcome screen and the chat share the id; talk to the visible one.
    box = page.locator("#message:visible").first
    box.click()
    box.fill(text)
    btn = page.locator("button.send-btn:visible").first
    btn.click()
