"""A20 (docs/spec/paridad/, CONTRATO_SDK_S3.md): "External application
installs package in clean CJS and ESM projects -> typed session turn stream
approval cancellation artifact flow works with declared peers".

The manifesto's rule (docs/spec/paridad/README.md) is that nothing closes on
a fake standing in for the mechanism under test. So this test does not call
into the SDK or the app's own routes directly: it starts a REAL Faustus
server (uvicorn in a subprocess, a temporary data dir, auth ON — no
LOCALHOST_BYPASS), mints a REAL bearer token through the running admin
session, packs `sdk/ts` with `npm pack` and installs that exact tarball
(`npm install ... <tgz>`, no other network access) into two clean Node
projects — one ESM, one CommonJS — copied from `sdk/ts/examples/`. Those
installed consumers are then run as separate `node` subprocesses that talk
to the server only through `faustus-sdk`'s public API and a bearer token,
exactly the way an outside integrator would.

Three journeys, matching CONTRATO_SDK_S3.md:

1. Full walk (ESM and CJS, both): a scripted local model reads then edits
   `calc.py` in a real workspace, the consumer answers the in-turn
   `tool_approval` `ask_user`, follows the turn to `[DONE]`, exports the
   session and verifies the download's sha256 against the artifact store's
   own metadata.
2. Cancellation (ESM only): the consumer cancels a live turn and confirms
   `turns.resume()` then throws `RunNotActiveError`.
3. Negative: a token minted with the `chat` profile (no `sessions` scope)
   is refused with 403 the moment it tries to create a session.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from tests.acceptance.conftest import record_evidence

REPO = Path(__file__).resolve().parents[2]
SDK = REPO / "sdk" / "ts"

# Copied from tests/e2e/test_agent_flows.py — the exact fenced-tool-call
# shape the local-model agent loop parses (see tests/e2e/fake_llm.py).
READ = '```read_file\n{"path": "calc.py"}\n```'
EDIT = '```edit_file\n{"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}\n```'

ADMIN_USER = "a20admin"
ADMIN_PASSWORD = "a20-admin-password-1"


# ---------------------------------------------------------------------------
# Small HTTP helpers (copied in spirit from tests/e2e/conftest.py — this
# file deliberately does not import that conftest, which skips this whole
# directory globally without ODYSSEUS_E2E).
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
    """(status, json_body, response_headers) — `response_headers` is the raw
    `email.message.Message`-like object urllib hands back, so header lookups
    (`.get("Set-Cookie")`) stay case-insensitive the way HTTP headers are;
    building a plain `dict` first (as an earlier version of this helper did)
    silently lower-cases every key and breaks exactly that lookup."""
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


def _put_json(url: str, data: dict, headers: Optional[dict] = None):
    return _request("PUT", url, data=data, headers=headers, json_body=True)


def _get(url: str, headers: Optional[dict] = None):
    return _request("GET", url, headers=headers)


def _make_workspace(base_dir: Path) -> Path:
    """A tiny pytest project the fake model edits — the exact same shape as
    tests/e2e/conftest.py's `workspace` fixture (the harness runs the
    project's own tests as part of verifying an edit; a workspace with no
    test suite at all is not the scenario that fixture — or this test —
    exercises)."""
    ws = base_dir / "ws"
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    return ws


def _script_fake_model(fake_base: str, responses: list, delay: float = 0.0) -> None:
    status, body, _ = _post_json(fake_base + "/_script", {"responses": responses, "delay": delay, "reset": True})
    assert status == 200 and body.get("ok") is True, (status, body)


def _run_consumer(node: str, project_dir: Path, kind: str, server: dict,
                   env_extra: dict, timeout: float = 60.0) -> Tuple[int, dict, str]:
    entry = "index.mjs" if kind == "esm" else "index.cjs"
    env = dict(os.environ)
    env.update({
        "FAUSTUS_URL": server["base"],
        "FAUSTUS_ENDPOINT_ID": server["endpoint_id"],
        "FAUSTUS_ENDPOINT_URL": server["endpoint_url"],
        "FAUSTUS_MODEL": "fake-coder",
    })
    env.update(env_extra)
    proc = subprocess.run(
        [node, entry], cwd=str(project_dir), env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, f"{kind}: consumer produced no stdout (stderr: {proc.stderr[-2000:]})"
    payload = json.loads(lines[-1])
    return proc.returncode, payload, proc.stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def node_bin():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        pytest.skip("node/npm not found in PATH — A20 needs a real installed external consumer")
    return node, npm


@pytest.fixture(scope="module")
def fake_model():
    from tests.e2e import fake_llm as fake_llm_mod
    port = _free_port()
    srv = fake_llm_mod.serve(port)
    yield {"base": f"http://127.0.0.1:{port}"}
    srv.shutdown()


@pytest.fixture(scope="module")
def sdk_tarball(node_bin, tmp_path_factory):
    """`npm run build && npm pack` in sdk/ts — the exact tarball the
    consumer projects install from below."""
    node, npm = node_bin
    dest = tmp_path_factory.mktemp("a20-sdk-tgz")

    build = subprocess.run([npm, "run", "build"], cwd=str(SDK), capture_output=True, text=True, timeout=120)
    assert build.returncode == 0, "npm run build failed:\n" + build.stdout + build.stderr

    pack = subprocess.run(
        [npm, "pack", "--pack-destination", str(dest)],
        cwd=str(SDK), capture_output=True, text=True, timeout=60,
    )
    assert pack.returncode == 0, "npm pack failed:\n" + pack.stdout + pack.stderr
    tgz_name = pack.stdout.strip().splitlines()[-1].strip()
    tgz_path = dest / tgz_name
    assert tgz_path.is_file(), f"expected {tgz_name} under {dest}, found {list(dest.iterdir())}"

    node_version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    npm_version = subprocess.run([npm, "--version"], capture_output=True, text=True).stdout.strip()

    return {
        "path": tgz_path,
        "name": tgz_name,
        "sha256": hashlib.sha256(tgz_path.read_bytes()).hexdigest(),
        "node_version": node_version,
        "npm_version": npm_version,
    }


@pytest.fixture(scope="module")
def consumer_projects(node_bin, sdk_tarball, tmp_path_factory):
    """`sdk/ts/examples/{esm,cjs}` copied into clean projects, with
    `faustus-sdk` installed from the packed tarball — no registry access."""
    node, npm = node_bin
    projects: Dict[str, dict] = {}
    for kind in ("esm", "cjs"):
        dest = tmp_path_factory.mktemp(f"a20-{kind}-project")
        shutil.copytree(SDK / "examples" / kind, dest, dirs_exist_ok=True)

        install_mode = "--offline"
        install = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--offline", str(sdk_tarball["path"])],
            cwd=str(dest), capture_output=True, text=True, timeout=120,
        )
        if install.returncode != 0:
            # --offline can fail on a registry probe npm insists on even for
            # a dependency-free local tarball install; --prefer-offline
            # still avoids the network for anything actually cached/local.
            install_mode = "--prefer-offline"
            install = subprocess.run(
                [npm, "install", "--no-audit", "--no-fund", "--prefer-offline", str(sdk_tarball["path"])],
                cwd=str(dest), capture_output=True, text=True, timeout=120,
            )
        assert install.returncode == 0, f"{kind}: npm install failed:\n" + install.stdout + install.stderr

        pkg_dir = dest / "node_modules" / "faustus-sdk"
        assert (pkg_dir / "package.json").is_file(), f"{kind}: faustus-sdk not installed under {pkg_dir}"
        assert (pkg_dir / "dist" / "esm" / "index.js").is_file(), f"{kind}: dist/esm/index.js missing"
        assert (pkg_dir / "dist" / "cjs" / "index.cjs").is_file(), f"{kind}: dist/cjs/index.cjs missing"

        projects[kind] = {"dir": dest, "install_mode": install_mode}
    return projects


@pytest.fixture(scope="module")
def a20_server(node_bin, fake_model):
    """A real Faustus server: AUTH_ENABLED=true, no localhost bypass, a
    temp data dir, the scripted local model as its only endpoint, and one
    `sessions`-scoped and one scopeless (`chat`) token minted through the
    real admin cookie session."""
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="faustus-a20-data-")
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
            "name": "a20-fake", "base_url": fake_model["base"] + "/v1",
            "skip_probe": "true", "endpoint_kind": "local",
        }, headers=auth_headers)
        assert status == 200, (status, ep)
        endpoint_id = ep.get("id") or (ep.get("endpoint") or {}).get("id")
        assert endpoint_id, ep

        # Best-effort background jobs (auto-skill-extraction, auto-memory)
        # fire off their own calls to the same scripted model on a real
        # agent turn — real server behaviour, but it races this test's own
        # exact-sequence script. They are orthogonal to A20 (session/turn/
        # approval/cancel/export/artifacts), so turn them off for the admin
        # user the SDK token is minted for, the same way an integrator who
        # wants deterministic runs would.
        for key in ("auto_skills", "auto_memory"):
            status, _body, _ = _put_json(base + f"/api/prefs/{key}", {"value": False}, headers=auth_headers)
            assert status == 200, (key, status, _body)

        # The walk needs one in-turn approval card to answer. Reading a file in
        # the bound workspace no longer counts as outside context, so an
        # explicit "fix calc.py" edits without asking; an argument rule with
        # action "ask" is how an integrator asks for a card on every edit.
        status, _body, _ = _put_json(base + "/api/tool-arg-rules", {"rules": [{
            "id": "a20-ask-before-edits", "tool": "edit_file", "arg": "path", "op": "equals",
            "value": "::no-path-matches::", "action": "ask",
            "note": "Every edit asks first in this test.",
        }]}, headers=auth_headers)
        assert status == 200, (status, _body)

        status, tok, _ = _post_form(base + "/api/tokens", {"name": "sdk-a20", "profile": "sdk"}, headers=auth_headers)
        assert status == 200, (status, tok)
        sdk_token = tok["token"]
        assert sdk_token.startswith("ody_"), tok

        status, tok2, _ = _post_form(base + "/api/tokens", {"name": "sdk-a20-denied", "profile": "chat"}, headers=auth_headers)
        assert status == 200, (status, tok2)
        denied_token = tok2["token"]
        assert denied_token.startswith("ody_"), tok2

        yield {
            "base": base,
            "cookie": cookie,
            "endpoint_id": endpoint_id,
            "endpoint_url": fake_model["base"] + "/v1",
            "sdk_token": sdk_token,
            "denied_token": denied_token,
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
# The test
# ---------------------------------------------------------------------------


@pytest.mark.acceptance("A20")
@pytest.mark.slow
def test_a20_external_sdk_consumer_full_walk_cancel_and_denied(
    request, tmp_path_factory, node_bin, fake_model, sdk_tarball, consumer_projects, a20_server,
):
    node, _npm = node_bin
    t0 = time.time()
    journeys: Dict[str, Any] = {}

    # ---- Recorrido 1: full walk, ESM and CJS, both --------------------
    for kind in ("esm", "cjs"):
        ws = _make_workspace(tmp_path_factory.mktemp(f"a20-run-ws-{kind}"))
        _script_fake_model(fake_model["base"], [READ, EDIT, "He corregido calc.py: ahora suma."])

        rc, payload, stderr = _run_consumer(
            node, consumer_projects[kind]["dir"], kind, a20_server,
            env_extra={
                "FAUSTUS_TOKEN": a20_server["sdk_token"],
                "FAUSTUS_WORKSPACE": str(ws),
                "FAUSTUS_MESSAGE": "Arregla la función add en calc.py",
            },
        )
        assert rc == 0, (kind, payload, stderr[-2000:])
        assert payload.get("ok") is True, payload
        assert payload.get("runId"), payload
        assert payload.get("events", {}).get("delta", 0) >= 1, payload
        assert payload.get("events", {}).get("ask_user", 0) >= 1, payload
        assert payload.get("approvals") == 1, payload
        assert payload.get("events", {}).get("tool_output", 0) >= 1, payload
        assert payload.get("end") == "done", payload
        assert payload.get("artifacts", 0) >= 1, payload
        assert payload.get("sha256Match") is True, payload
        assert (ws / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n", \
            "the edit must have actually landed on disk via the token-approved turn"
        journeys[kind] = payload

        # Server-side, with the admin cookie: the session a bearer token
        # created is visible under GET /api/sessions (its owner is the
        # token's owner) and its history has the message the consumer sent.
        sid = payload["sessionId"]
        status, sessions_body, _ = _get(a20_server["base"] + "/api/sessions", headers={"Cookie": a20_server["cookie"]})
        assert status == 200, sessions_body
        assert any(s.get("id") == sid for s in sessions_body), (sid, sessions_body)
        status, history_body, _ = _get(a20_server["base"] + f"/api/history/{sid}", headers={"Cookie": a20_server["cookie"]})
        assert status == 200, history_body
        assert "Arregla la función add en calc.py" in json.dumps(history_body, ensure_ascii=False), history_body

    # ---- Recorrido 2: cancel (ESM is enough) ---------------------------
    cancel_text = "".join(
        f"Paso {i}: reviso con calma una parte distinta del archivo y anoto lo que hace. " for i in range(1, 80)
    )
    _script_fake_model(fake_model["base"], [cancel_text], delay=0.2)
    rc, cancel_payload, stderr = _run_consumer(
        node, consumer_projects["esm"]["dir"], "esm", a20_server,
        env_extra={
            "FAUSTUS_TOKEN": a20_server["sdk_token"],
            "FAUSTUS_MESSAGE": "Cuenta, paso a paso y muy despacio, qué hace este código.",
            "MODE": "cancel",
        },
    )
    assert rc == 0, (cancel_payload, stderr[-2000:])
    assert cancel_payload.get("ok") is True, cancel_payload
    assert (cancel_payload.get("stop") or {}).get("stopped") is True, cancel_payload
    assert cancel_payload.get("end") in ("stopped", "run_gone", "done"), cancel_payload
    journeys["cancel"] = cancel_payload

    # ---- Recorrido 3: negative — a `chat`-only token is refused -------
    rc, denied_payload, stderr = _run_consumer(
        node, consumer_projects["esm"]["dir"], "esm", a20_server,
        env_extra={"FAUSTUS_TOKEN": a20_server["denied_token"], "MODE": "denied"},
    )
    assert rc == 0, (denied_payload, stderr[-2000:])
    assert denied_payload.get("ok") is True, denied_payload
    assert denied_payload.get("status") == 403, denied_payload
    assert "sessions" in (denied_payload.get("detail") or ""), denied_payload
    journeys["denied"] = denied_payload

    elapsed_s = time.time() - t0

    record_evidence(
        request,
        tgz_name=sdk_tarball["name"],
        tgz_sha256=sdk_tarball["sha256"],
        node_version=sdk_tarball["node_version"],
        npm_version=sdk_tarball["npm_version"],
        npm_install_mode={k: v["install_mode"] for k, v in consumer_projects.items()},
        journeys=journeys,
        session_ids={kind: journeys[kind].get("sessionId") for kind in ("esm", "cjs")},
        run_ids={kind: journeys[kind].get("runId") for kind in ("esm", "cjs")},
        sdk_version="0.1.0",
        elapsed_s=round(elapsed_s, 1),
        model="scripted",
        cost_status="unpriced",
        total_cost=0,
    )
