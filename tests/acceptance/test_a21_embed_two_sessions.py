"""A21 (docs/spec/paridad/, acceptance_cases.json): "Embed two sessions in
an existing application" -> "Style routing auth and session state remain
isolated and accessible".

`studio/embed/` (`@faustus/embed`) ships a `<faustus-chat>` Web Component
(Shadow DOM, its own `faustus-sdk` client) that a host page can drop in more
than once. Three layers close this case, matching the manifesto's rule that
nothing closes on a class existing or a fake standing in for the mechanism
under test:

1. `test_a21_embed_check_two_instances_isolated_in_dom` — runs the REAL
   built bundle (`studio/embed/dist/faustus-embed.js`, esbuild-bundled from
   `studio/embed/src/*.ts`, which pulls `sdk/ts/src/*.ts` in directly) in a
   real DOM (`happy-dom`) via `node studio/checks/embed.check.mjs`, a
   subprocess this test does not fake or reimplement — it asserts the exit
   code and reads the check's own pass/fail lines.
2. `test_a21_two_real_sessions_two_tokens_do_not_see_each_other` — a REAL
   Faustus server (uvicorn subprocess, auth on) with two `sessions`-scoped
   tokens for two DIFFERENT users, proving over real HTTP (not through the
   embed component) that a session created under one token is invisible
   (404) to the other token, the hard security property the Web Component
   depends on. This alone is enough to close A21's "session state remain
   isolated" half even if a browser is unavailable in some future CI image.
3. `test_a21_playwright_two_faustus_chat_instances_against_real_server` —
   opens `studio/embed/example.html` (served statically) in a REAL Chromium
   (Playwright) against the REAL server from (2), with a scripted local
   model, and drives both `<faustus-chat>` panels through an actual turn
   each, checking the rendered Shadow DOM text of one panel never contains
   the other's reply and that neither panel's light-DOM markup or
   `localStorage` ever contains a bearer token. If Chromium cannot launch in
   this environment, this one test is `xfail(strict=True)` with the caught
   launch error as its reason — (1) and (2) still close the case with real,
   unfaked mechanisms.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import pytest

from tests.acceptance.conftest import record_evidence

REPO = Path(__file__).resolve().parents[2]
EMBED_DIR = REPO / "studio" / "embed"
CHECK_SCRIPT = REPO / "studio" / "checks" / "embed.check.mjs"

ADMIN_USER = "a21admin"
ADMIN_PASSWORD = "a21-admin-password-1"


# ---------------------------------------------------------------------------
# Small HTTP helpers (same shape as tests/acceptance/test_a20_external_sdk_
# consumer.py's — deliberately not imported from there to keep this file's
# ownership self-contained per the lot's file-ownership rule).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 120.0) -> None:
    t0 = time.time()
    last: Optional[Exception] = None
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up within {timeout}s: {last}")


def _request(method: str, url: str, data: Optional[dict] = None, headers: Optional[dict] = None,
             json_body: bool = False):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        if json_body:
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        else:
            body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
            return r.status, payload, r.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw}
        return e.code, payload, e.headers


def _post_form(url: str, data: dict, headers: Optional[dict] = None):
    return _request("POST", url, data=data, headers=headers, json_body=False)


def _post_json(url: str, data: dict, headers: Optional[dict] = None):
    return _request("POST", url, data=data, headers=headers, json_body=True)


def _get(url: str, headers: Optional[dict] = None):
    return _request("GET", url, headers=headers)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def node_bin():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found in PATH — A21 needs node for the embed check")
    return node


@pytest.fixture(scope="module")
def embed_bundle(node_bin):
    """Builds the real `@faustus/embed` bundle once for this module (the
    check script would build it lazily too, but building it once here and
    asserting success gives a clearer failure than a check-script crash
    would)."""
    build = subprocess.run(
        [node_bin, "build.mjs"], cwd=str(EMBED_DIR), capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, "esbuild of @faustus/embed failed:\n" + build.stdout + build.stderr
    bundle = EMBED_DIR / "dist" / "faustus-embed.js"
    assert bundle.is_file(), f"expected {bundle} after build"
    return bundle


@pytest.fixture(scope="module")
def fake_model():
    from tests.e2e import fake_llm as fake_llm_mod
    port = _free_port()
    srv = fake_llm_mod.serve(port)
    yield {"base": f"http://127.0.0.1:{port}"}
    srv.shutdown()


class _StaticServerThread:
    """Serves `studio/embed/` (example.html + dist/) on its own origin —
    deliberately a DIFFERENT port than the Faustus server, so a browser
    request from example.html to the API is genuinely cross-origin and
    exercises the real CORS path a host application would hit. Started
    BEFORE the Faustus server below so its origin is known in time to go
    into that server's `ALLOWED_ORIGINS` at process start (Starlette's
    `CORSMiddleware` matches `Origin` by exact string, port included — a
    wildcard-free, honest test of the embed's own documented CORS
    requirement, not a bypass of it)."""

    def __init__(self, directory: Path):
        self.port = _free_port()
        handler = lambda *a, **kw: SimpleHTTPRequestHandler(*a, directory=str(directory), **kw)  # noqa: E731
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        base = f"http://127.0.0.1:{self.port}"
        _wait_http(base + "/example.html", timeout=15)
        return base

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def static_embed_server():
    server = _StaticServerThread(EMBED_DIR)
    base = server.start()
    yield base
    server.stop()


@pytest.fixture(scope="module")
def a21_server(node_bin, fake_model, static_embed_server):
    """A real Faustus server: AUTH_ENABLED=true, no localhost bypass, a temp
    data dir, the scripted local model as its only endpoint, `ALLOWED_ORIGINS`
    widened to `static_embed_server`'s real origin (the embed's documented
    CORS requirement — see `studio/embed/README.md`), and TWO
    `sessions`-scoped tokens minted for TWO different accounts — the shape
    A21 actually needs (two people embedding two isolated sessions), unlike
    A20's one-admin two-token (one `sessions`, one `chat`) split."""
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="faustus-a21-data-")
    env = dict(os.environ)
    env.update({
        "ODYSSEUS_DATA_DIR": data_dir,
        "DATABASE_URL": "sqlite:///" + (data_dir.replace("\\", "/") + "/app.db"),
        "APP_PORT": str(port),
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "ODYSSEUS_INPROCESS_POLLERS": "0",
        "ODYSSEUS_INPROCESS_TASKS": "0",
        "ODYSSEUS_STARTUP_WARMUPS": "0",
        # The static server for example.html (see `_StaticServerThread`)
        # runs on a different origin — this is the wiring A21's design
        # calls for (studio/embed/README.md's "CORS" section): no new
        # server setting was needed because app.py already reads
        # ALLOWED_ORIGINS from the environment at import time.
        "ALLOWED_ORIGINS": f"http://127.0.0.1,http://localhost,{static_embed_server}",
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
        _wait_http(base + "/api/health", timeout=120)

        status, body, _ = _post_json(base + "/api/auth/setup", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert status == 200 and body.get("ok") is True, (status, body)
        status, body, headers = _post_json(base + "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert status == 200 and body.get("ok") is True, (status, body)
        set_cookie = headers.get("Set-Cookie") or ""
        cookie = set_cookie.split(";", 1)[0]
        assert cookie.startswith("odysseus_session="), set_cookie
        auth_headers = {"Cookie": cookie}

        status, ep, _ = _post_form(base + "/api/model-endpoints", {
            "name": "a21-fake", "base_url": fake_model["base"] + "/v1",
            "skip_probe": "true", "endpoint_kind": "local",
        }, headers=auth_headers)
        assert status == 200, (status, ep)
        endpoint_id = ep.get("id") or (ep.get("endpoint") or {}).get("id")
        assert endpoint_id, ep

        for key in ("auto_skills", "auto_memory"):
            status, _body, _ = _request("PUT", base + f"/api/prefs/{key}", data={"value": False},
                                         headers=auth_headers, json_body=True)
            assert status == 200, (key, status, _body)

        # Two DIFFERENT bearer tokens, each OWNED BY A DIFFERENT ACCOUNT —
        # this server's token model attributes every token to the account
        # that minted it (`routes/api_token_routes.py::create_token`:
        # `owner = get_current_user(request)`, i.e. the caller's own
        # session, not a token-level tenant), and `GET /api/sessions`
        # filters strictly by that owner (`routes/session_routes.py::
        # list_sessions` -> `owner_filter(q, DbSession, user)`, unconditional
        # — unlike the model-endpoint picker, there is no admin bypass
        # here). So two tokens minted by the SAME admin share that admin's
        # session list by design; proving real isolation needs two real
        # accounts. `token_a` stays on the original admin; `token_b` is
        # minted by a second admin account created here for exactly this
        # (token minting itself is `require_admin`-gated, so the second
        # account has to be an admin too — that does not weaken this test,
        # since `list_sessions`'s owner filter has no admin exception).
        status, tok_a, _ = _post_form(base + "/api/tokens", {"name": "embed-a21-a", "profile": "sdk"}, headers=auth_headers)
        assert status == 200, (status, tok_a)
        token_a = tok_a["token"]
        assert token_a.startswith("ody_"), tok_a

        second_user = "a21admin2"
        second_password = "a21-admin-password-2"
        status, created, _ = _post_json(
            base + "/api/auth/users",
            {"username": second_user, "password": second_password, "is_admin": True},
            headers=auth_headers,
        )
        assert status == 200 and created.get("ok") is True, created

        status, login_body, login_headers = _post_json(
            base + "/api/auth/login", {"username": second_user, "password": second_password},
        )
        assert status == 200 and login_body.get("ok") is True, login_body
        set_cookie_b = login_headers.get("Set-Cookie") or ""
        cookie_b = set_cookie_b.split(";", 1)[0]
        assert cookie_b.startswith("odysseus_session="), set_cookie_b
        auth_headers_b = {"Cookie": cookie_b}

        status, tok_b, _ = _post_form(base + "/api/tokens", {"name": "embed-a21-b", "profile": "sdk"}, headers=auth_headers_b)
        assert status == 200, (status, tok_b)
        token_b = tok_b["token"]
        assert token_b.startswith("ody_"), tok_b
        assert token_a != token_b
        assert tok_b.get("owner") == second_user, tok_b

        yield {
            "base": base,
            "cookie": cookie,
            "endpoint_id": endpoint_id,
            "endpoint_url": fake_model["base"] + "/v1",
            "token_a": token_a,
            "token_b": token_b,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. The node/happy-dom check
# ---------------------------------------------------------------------------


@pytest.mark.acceptance("A21")
def test_a21_embed_check_two_instances_isolated_in_dom(request, node_bin, embed_bundle):
    proc = subprocess.run(
        [node_bin, str(CHECK_SCRIPT)], cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    record_evidence(
        request,
        check_script=str(CHECK_SCRIPT.relative_to(REPO)),
        check_returncode=proc.returncode,
        check_stdout_tail=proc.stdout[-3000:],
    )
    assert proc.returncode == 0, (
        f"studio/checks/embed.check.mjs failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ALL CHECKS PASSED" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# 2. Real server, real HTTP: two tokens' sessions never see each other
# ---------------------------------------------------------------------------


@pytest.mark.acceptance("A21")
def test_a21_two_real_sessions_two_tokens_do_not_see_each_other(request, a21_server):
    base = a21_server["base"]
    hdr_a = {"Authorization": f"Bearer {a21_server['token_a']}"}
    hdr_b = {"Authorization": f"Bearer {a21_server['token_b']}"}

    status, session_a, _ = _post_form(
        base + "/api/session",
        {"name": "A21 panel A", "endpoint_id": a21_server["endpoint_id"], "skip_validation": "true"},
        headers=hdr_a,
    )
    assert status == 200, session_a
    sid_a = session_a["id"]

    status, session_b, _ = _post_form(
        base + "/api/session",
        {"name": "A21 panel B", "endpoint_id": a21_server["endpoint_id"], "skip_validation": "true"},
        headers=hdr_b,
    )
    assert status == 200, session_b
    sid_b = session_b["id"]

    assert sid_a != sid_b

    # Token A's own session list must show A, not B; token B's must show B,
    # not A — this is the exact server-side guarantee `<faustus-chat>` (and
    # every other embed instance) relies on for isolation.
    status, sessions_a, _ = _get(base + "/api/sessions", headers=hdr_a)
    assert status == 200, sessions_a
    ids_a = {s["id"] for s in sessions_a}
    assert sid_a in ids_a, (sid_a, ids_a)
    assert sid_b not in ids_a, "token A's session list leaked token B's session"

    status, sessions_b, _ = _get(base + "/api/sessions", headers=hdr_b)
    assert status == 200, sessions_b
    ids_b = {s["id"] for s in sessions_b}
    assert sid_b in ids_b, (sid_b, ids_b)
    assert sid_a not in ids_b, "token B's session list leaked token A's session"

    # Cross-token history reads are refused (404/403), never silently
    # served — the concrete "isolated and accessible" property from A21's
    # own expectation string, proven over real HTTP against real routes,
    # real session ownership and a real auth matrix (no fakes anywhere in
    # this test except the LLM endpoint neither session ever calls here).
    status_cross_a, body_cross_a, _ = _get(base + f"/api/history/{sid_b}", headers=hdr_a)
    assert status_cross_a in (403, 404), (status_cross_a, body_cross_a)

    status_cross_b, body_cross_b, _ = _get(base + f"/api/history/{sid_a}", headers=hdr_b)
    assert status_cross_b in (403, 404), (status_cross_b, body_cross_b)

    record_evidence(
        request,
        server_base=base,
        session_a=sid_a,
        session_b=sid_b,
        cross_read_a_on_b_status=status_cross_a,
        cross_read_b_on_a_status=status_cross_b,
    )


# ---------------------------------------------------------------------------
# 3. Playwright: two <faustus-chat> panels against the real server
# ---------------------------------------------------------------------------


def _playwright_chromium_launch_error() -> Optional[str]:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return f"playwright not installed: {e}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            browser.close()
        return None
    except Exception as e:  # noqa: BLE001 — any launch failure closes this with xfail, not an error
        return f"{type(e).__name__}: {e}"


_LAUNCH_ERROR = _playwright_chromium_launch_error()


@pytest.mark.acceptance("A21")
@pytest.mark.xfail(condition=_LAUNCH_ERROR is not None, strict=True, reason=str(_LAUNCH_ERROR))
def test_a21_playwright_two_faustus_chat_instances_against_real_server(request, embed_bundle, a21_server, static_embed_server):
    if _LAUNCH_ERROR is not None:
        pytest.fail(_LAUNCH_ERROR)  # pragma: no cover — turned into the xfail above

    from playwright.sync_api import sync_playwright

    static_base = static_embed_server

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(f"{static_base}/example.html")
        page.wait_for_selector("#chat-a")
        page.wait_for_selector("#chat-b")

        page.evaluate(
            """([server, tokenA, tokenB]) => {
                const a = document.getElementById('chat-a');
                const b = document.getElementById('chat-b');
                a.server = server; b.server = server;
                a.token = tokenA; b.token = tokenB;
            }""",
            [a21_server["base"], a21_server["token_a"], a21_server["token_b"]],
        )

        # Type + send in panel A (Enter key — exercises the same
        # keyboard path studio/checks/embed.check.mjs checks headlessly).
        textbox_a = page.locator("#chat-a").locator("[role=textbox]")
        textbox_a.click()
        textbox_a.type("Say the single word PANELA and stop.")
        textbox_a.press("Enter")

        textbox_b = page.locator("#chat-b").locator("[role=textbox]")
        textbox_b.click()
        textbox_b.type("Say the single word PANELB and stop.")
        textbox_b.press("Enter")

        page.wait_for_function(
            """() => {
                const a = document.getElementById('chat-a').shadowRoot.querySelector('.fc-transcript');
                const b = document.getElementById('chat-b').shadowRoot.querySelector('.fc-transcript');
                return a && b && a.textContent.length > 5 && b.textContent.length > 5;
            }""",
            timeout=30000,
        )
        # Let both turns actually finish (agent_terminal/[DONE]).
        page.wait_for_timeout(1500)

        transcript_a = page.evaluate(
            "document.getElementById('chat-a').shadowRoot.querySelector('.fc-transcript').textContent"
        )
        transcript_b = page.evaluate(
            "document.getElementById('chat-b').shadowRoot.querySelector('.fc-transcript').textContent"
        )
        outer_a = page.evaluate("document.getElementById('chat-a').outerHTML")
        outer_b = page.evaluate("document.getElementById('chat-b').outerHTML")
        body_html = page.evaluate("document.body.innerHTML")
        local_storage_dump = page.evaluate("JSON.stringify(window.localStorage)")

        browser.close()

    token_a = a21_server["token_a"]
    token_b = a21_server["token_b"]

    assert "PANELA" in transcript_a, transcript_a
    assert "PANELB" not in transcript_a, transcript_a
    assert "PANELB" in transcript_b, transcript_b
    assert "PANELA" not in transcript_b, transcript_b

    for blob, name in ((outer_a, "chat-a outerHTML"), (outer_b, "chat-b outerHTML"), (body_html, "document.body")):
        assert token_a not in blob, f"{name} leaked token A"
        assert token_b not in blob, f"{name} leaked token B"
    assert token_a not in local_storage_dump and token_b not in local_storage_dump, local_storage_dump

    record_evidence(
        request,
        server_base=a21_server["base"],
        static_base=static_base,
        transcript_a_len=len(transcript_a),
        transcript_b_len=len(transcript_b),
        console_errors=console_errors[:10],
    )
