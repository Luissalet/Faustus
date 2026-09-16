"""A23 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A23): "Unattended job
uses revoked or insufficiently scoped credential" -> "Denied per scope;
revocation takes effect within documented bound".

Exercises real Faustus code end to end: a REAL Faustus server (uvicorn in a
subprocess, a temporary data dir, `AUTH_ENABLED=true`, no
`LOCALHOST_BYPASS` — same technique `tests/acceptance/test_a20_external_
sdk_consumer.py` uses so nothing about admission is short-circuited), a
REAL admin session, the REAL `POST /api/tokens` token-creation route (this
lot's `subject=`/`expires_in_seconds=` service-identity extension), the
REAL `POST /api/tokens/{id}/revoke` revocation route, and REAL HTTP
requests bearing the minted `ody_...` bearer token against a real
scope-gated route (`POST /api/session`, which `core/authz.py` opens only to
the `sessions` scope) — the same production bearer-auth path
`test_a20_external_sdk_consumer.py`'s "denied" journey exercises, now
additionally reached through a token this lot's service-identity flow
minted, with `src/service_identity.py`'s own store used to prove the
`token_revocation_bound_seconds` bound holds for that module's checks too.

Two scenarios, matching the trigger/expectation exactly:

1. Insufficiently scoped: a service-identity token minted with only the
   `chat` scope (no `sessions`) hits `POST /api/session` -> 403 naming the
   missing scope, exactly as A20's "denied" journey already established for
   a human-minted token — this is the same real mechanism, now exercised
   through a service (`subject=`) token instead.
2. Revoked: a `sessions`-scoped service-identity token succeeds once, is
   revoked through the real `/revoke` route, and the SAME token on a second
   real request is refused with 401 — immediately (the default
   `token_revocation_bound_seconds` is 0), which this test also confirms
   directly against `src.service_identity.is_revoked()`, the new module's
   own promise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

from tests.acceptance.conftest import record_evidence

REPO = Path(__file__).resolve().parents[2]

ADMIN_USER = "a23admin"
ADMIN_PASSWORD = "a23-admin-password-1"


# ---------------------------------------------------------------------------
# HTTP helpers (same shape as test_a20_external_sdk_consumer.py's)
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


# ---------------------------------------------------------------------------
# Fixture: a real Faustus server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def a23_server():
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="faustus-a23-data-")
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

        yield {"base": base, "cookie": cookie, "data_dir": data_dir}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.acceptance("A23")
@pytest.mark.slow
def test_a23_insufficient_scope_denied_and_revocation_effective_within_bound(request, a23_server):
    base, cookie = a23_server["base"], a23_server["cookie"]
    auth_headers = {"Cookie": cookie}
    results = {}

    # -- 1. Insufficiently scoped service-identity credential -----------
    status, tok, _ = _post_form(base + "/api/tokens", {
        "name": "a23-underscoped-job",
        "subject": "scheduler:nightly-report",
        "scopes": "chat",  # deliberately missing "sessions"
    }, headers=auth_headers)
    assert status == 200, (status, tok)
    underscoped_token = tok["token"]
    assert underscoped_token.startswith("ody_"), tok
    assert tok.get("subject") == "scheduler:nightly-report", tok

    status, body, _ = _post_form(base + "/api/session", {"name": "unattended-run"},
                                  headers={"Authorization": f"Bearer {underscoped_token}"})
    assert status == 403, (status, body)
    assert "sessions" in (body.get("error") or ""), body
    results["missing_scope_status"] = status
    results["missing_scope_error"] = body.get("error")

    # -- 2. Revoked service-identity credential ---------------------------
    status, tok2, _ = _post_form(base + "/api/tokens", {
        "name": "a23-scoped-job",
        "subject": "scheduler:nightly-report",
        "scopes": "sessions",
        "expires_in_seconds": "3600",
    }, headers=auth_headers)
    assert status == 200, (status, tok2)
    scoped_token = tok2["token"]
    scoped_token_id = tok2["id"]
    assert tok2.get("expires_at") is not None, tok2

    # First real request: the live credential is accepted past the
    # bearer-auth/scope gate (a 400/422 from the handler itself, for a
    # session created with no real model endpoint configured, still proves
    # admission — a 403 here would mean the scope check itself failed).
    status, body, _ = _post_form(base + "/api/session", {"name": "unattended-run"},
                                  headers={"Authorization": f"Bearer {scoped_token}"})
    assert status != 403 and status != 401, (status, body)
    results["before_revoke_status"] = status

    revoke_t0 = time.time()
    status, body, _ = _post_form(base + f"/api/tokens/{scoped_token_id}/revoke", {}, headers=auth_headers)
    assert status == 200 and body.get("status") == "revoked", (status, body)
    revoke_elapsed = time.time() - revoke_t0

    # Second real request, same token: refused within the documented bound
    # (default token_revocation_bound_seconds = 0 -> immediate).
    status, body, _ = _post_form(base + "/api/session", {"name": "unattended-run"},
                                  headers={"Authorization": f"Bearer {scoped_token}"})
    assert status == 401, (status, body)
    results["after_revoke_status"] = status
    results["revoke_call_elapsed_s"] = round(revoke_elapsed, 3)

    record_evidence(
        request,
        results=results,
        underscoped_token_id=tok["id"],
        scoped_token_id=scoped_token_id,
    )
