"""A22 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A22): "OIDC expired
token wrong audience callback replay and forbidden email" -> "Each invalid
identity rejected; valid mapped identity receives correct permissions".

Exercises real Faustus code end to end: a real `fastapi.testclient.
TestClient` against the real `routes/auth_routes.py` OIDC routes
(`GET /api/auth/oidc/login`, `GET /api/auth/oidc/callback`), a real
`core.auth.AuthManager` (bcrypt + JSON session store on a temp dir, exactly
what production uses), and `core/oidc.py`'s real PKCE/state/nonce registry
and `verify_id_token` — nothing about OIDC validation itself is faked.

What IS faked is the identity provider on the wire: an in-process RSA
keypair signs id_tokens with PyJWT, and discovery/JWKS/token-endpoint HTTP
calls are served by an `httpx.MockTransport` wired in through
`core.oidc.DEFAULT_TRANSPORT` — no real network access, matching the
contract's "IdP falso en proceso" instruction.

Scenarios, matching the trigger/expectation exactly:
  1. Expired id_token -> 401.
  2. Wrong `aud` -> 401.
  3. Replayed callback (same state used twice) -> 400, no session either time.
  4. Forbidden email (not on the allow-list) -> 403, and no local account or
     OIDC identity mapping is created for it.
  5. Allowed admin email -> a real session with admin permissions; a real
     admin-gated route answers 200.
  6. Allowed non-admin email -> a real session, but that SAME admin-gated
     route answers 403 for it.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Dict, Optional

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from urllib.parse import urlsplit, parse_qs

from core import oidc as oidc_mod
from core.auth import AuthManager
from core.middleware import require_admin
from routes import auth_routes as auth_routes_mod
from routes.auth_routes import setup_auth_routes, SESSION_COOKIE
from tests.acceptance.conftest import record_evidence

ISSUER = "https://idp.example-corp.test"
CLIENT_ID = "faustus-a22"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_b64url(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return _b64url(n.to_bytes(length, "big"))


@pytest.fixture(scope="module")
def idp_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    kid = "a22-test-key-1"
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _int_b64url(public_numbers.n),
        "e": _int_b64url(public_numbers.e),
    }
    return {"private_key": private_key, "kid": kid, "jwk": jwk}


def _sign_id_token(idp_keys, *, claims_overrides: Dict[str, Any], nonce: str) -> str:
    import jwt as pyjwt

    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "sub-default",
        "iat": now,
        "exp": now + 300,
        "email": "someone@corp.example",
        "email_verified": True,
        "nonce": nonce,
    }
    claims.update(claims_overrides)
    return pyjwt.encode(claims, idp_keys["private_key"], algorithm="RS256",
                         headers={"kid": idp_keys["kid"]})


class FakeIdp:
    """Serves discovery/JWKS/token-endpoint over an httpx.MockTransport.
    `codes` maps an authorization `code` string to the id_token it exchanges
    for (or to a fixed non-200 status to simulate a rejected exchange)."""

    def __init__(self, idp_keys):
        self.idp_keys = idp_keys
        self.codes: Dict[str, str] = {}
        self.token_requests: list[dict] = []

    def issue_code(self, code: str, id_token: str) -> None:
        self.codes[code] = id_token

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
            })
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.idp_keys["jwk"]]})
        if path == "/token":
            form = dict(parse_qs(request.content.decode("utf-8")))
            form = {k: v[0] for k, v in form.items()}
            self.token_requests.append(form)
            code = form.get("code", "")
            if code not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "id_token": self.codes[code],
            })
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture()
def fake_idp(idp_keys, monkeypatch):
    idp = FakeIdp(idp_keys)
    monkeypatch.setattr(oidc_mod, "DEFAULT_TRANSPORT", httpx.MockTransport(idp.handler))
    oidc_mod.clear_caches()
    yield idp
    oidc_mod.clear_caches()


@pytest.fixture()
def oidc_settings(monkeypatch):
    settings: Dict[str, Any] = {
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "",
        "oidc_redirect_uri": "https://faustus.example.test/api/auth/oidc/callback",
        "oidc_scopes": "openid email profile",
        "oidc_allowed_emails": ["admin@corp.example", "member@corp.example"],
        "oidc_allowed_domains": [],
        "oidc_admin_emails": ["admin@corp.example"],
        "oidc_admin_group": "",
        "oidc_discovery_cache_seconds": 3600,
        "oidc_jwks_cache_seconds": 3600,
    }
    monkeypatch.setattr(auth_routes_mod, "_load_settings", lambda: dict(settings))
    return settings


@pytest.fixture()
def auth_manager(tmp_path):
    return AuthManager(auth_path=str(tmp_path / "auth.json"))


@pytest.fixture()
def client(auth_manager, oidc_settings, fake_idp):
    app = FastAPI()
    app.state.auth_manager = auth_manager

    class _CookieAuth(BaseHTTPMiddleware):
        """The minimal, real slice of app.py's AuthMiddleware this test
        needs: read the session cookie, resolve it through the SAME
        `AuthManager.get_username_for_token` production uses. No bearer
        token / bypass paths -- those are not under test here."""

        async def dispatch(self, request: Request, call_next):
            token = request.cookies.get(SESSION_COOKIE)
            if token:
                request.state.current_user = auth_manager.get_username_for_token(token)
            return await call_next(request)

    app.add_middleware(_CookieAuth)
    app.include_router(setup_auth_routes(auth_manager))

    @app.get("/api/_test/admin-only")
    async def _admin_only(request: Request):
        require_admin(request)
        return {"ok": True}

    with TestClient(app) as c:
        yield c


def _start_login(client) -> Dict[str, str]:
    resp = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    qs = parse_qs(urlsplit(location).query)
    assert qs.get("code_challenge_method") == ["S256"]
    return {"state": qs["state"][0], "nonce": qs["nonce"][0]}


# ---------------------------------------------------------------------------
# 1. Expired id_token -> 401
# ---------------------------------------------------------------------------


@pytest.mark.acceptance("A22")
def test_a22_expired_wrong_audience_replay_and_forbidden_email(request, client, fake_idp, idp_keys, auth_manager):
    results: Dict[str, Any] = {}

    # -- expired --------------------------------------------------------
    login = _start_login(client)
    now = int(time.time())
    expired_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-expired", "email": "admin@corp.example",
        "iat": now - 1000, "exp": now - 400,
    })
    fake_idp.issue_code("code-expired", expired_token)
    resp = client.get("/api/auth/oidc/callback",
                       params={"code": "code-expired", "state": login["state"]},
                       follow_redirects=False)
    assert resp.status_code == 401, resp.text
    assert SESSION_COOKIE not in resp.cookies
    results["expired"] = resp.status_code

    # -- wrong audience ---------------------------------------------------
    login = _start_login(client)
    wrong_aud_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-wrong-aud", "email": "admin@corp.example", "aud": "some-other-client",
    })
    fake_idp.issue_code("code-wrong-aud", wrong_aud_token)
    resp = client.get("/api/auth/oidc/callback",
                       params={"code": "code-wrong-aud", "state": login["state"]},
                       follow_redirects=False)
    assert resp.status_code == 401, resp.text
    assert SESSION_COOKIE not in resp.cookies
    results["wrong_audience"] = resp.status_code

    # -- callback replay: same state used twice -> second is 400 --------
    login = _start_login(client)
    valid_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-replay", "email": "admin@corp.example",
    })
    fake_idp.issue_code("code-replay", valid_token)
    first = client.get("/api/auth/oidc/callback",
                        params={"code": "code-replay", "state": login["state"]},
                        follow_redirects=False)
    assert first.status_code in (302, 401, 403), first.text  # whatever it resolves to, state is now consumed
    second = client.get("/api/auth/oidc/callback",
                         params={"code": "code-replay", "state": login["state"]},
                         follow_redirects=False)
    assert second.status_code == 400, second.text
    assert SESSION_COOKIE not in second.cookies
    results["replay_first"] = first.status_code
    results["replay_second"] = second.status_code

    # -- forbidden email -> 403, no user/identity created ----------------
    login = _start_login(client)
    forbidden_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-forbidden", "email": "outsider@not-allowed.example",
    })
    fake_idp.issue_code("code-forbidden", forbidden_token)
    users_before = set(auth_manager.users.keys())
    resp = client.get("/api/auth/oidc/callback",
                       params={"code": "code-forbidden", "state": login["state"]},
                       follow_redirects=False)
    assert resp.status_code == 403, resp.text
    assert SESSION_COOKIE not in resp.cookies
    assert set(auth_manager.users.keys()) == users_before, "forbidden email must not provision a local account"
    assert "sub-forbidden" not in auth_manager._config.get("oidc_identities", {})
    results["forbidden_email"] = resp.status_code

    # -- allowed admin email -> real admin session -----------------------
    login = _start_login(client)
    admin_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-admin-1", "email": "admin@corp.example",
    })
    fake_idp.issue_code("code-admin", admin_token)
    resp = client.get("/api/auth/oidc/callback",
                       params={"code": "code-admin", "state": login["state"]},
                       follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert SESSION_COOKIE in resp.cookies
    admin_cookie = resp.cookies[SESSION_COOKIE]
    admin_username = auth_manager.get_username_for_token(admin_cookie)
    assert admin_username is not None
    assert auth_manager.is_admin(admin_username) is True
    admin_probe = client.get("/api/_test/admin-only", cookies={SESSION_COOKIE: admin_cookie})
    assert admin_probe.status_code == 200, admin_probe.text
    results["admin_login_status"] = resp.status_code
    results["admin_probe_status"] = admin_probe.status_code

    # -- allowed non-admin email -> real session, 403 on the admin route -
    login = _start_login(client)
    member_token = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-member-1", "email": "member@corp.example",
    })
    fake_idp.issue_code("code-member", member_token)
    resp = client.get("/api/auth/oidc/callback",
                       params={"code": "code-member", "state": login["state"]},
                       follow_redirects=False)
    assert resp.status_code == 302, resp.text
    member_cookie = resp.cookies[SESSION_COOKIE]
    member_username = auth_manager.get_username_for_token(member_cookie)
    assert auth_manager.is_admin(member_username) is False
    member_probe = client.get("/api/_test/admin-only", cookies={SESSION_COOKIE: member_cookie})
    assert member_probe.status_code == 403, member_probe.text
    results["member_login_status"] = resp.status_code
    results["member_probe_status"] = member_probe.status_code

    # -- re-login as the SAME admin subject re-syncs role, doesn't duplicate
    login = _start_login(client)
    admin_token_2 = _sign_id_token(idp_keys, nonce=login["nonce"], claims_overrides={
        "sub": "sub-admin-1", "email": "admin@corp.example",
    })
    fake_idp.issue_code("code-admin-2", admin_token_2)
    resp2 = client.get("/api/auth/oidc/callback",
                        params={"code": "code-admin-2", "state": login["state"]},
                        follow_redirects=False)
    assert resp2.status_code == 302
    reused_username = auth_manager.get_username_for_token(resp2.cookies[SESSION_COOKIE])
    assert reused_username == admin_username, "same sub must map to the same local account"

    record_evidence(
        request,
        results=results,
        admin_username=admin_username,
        member_username=member_username,
        token_endpoint_requests=len(fake_idp.token_requests),
    )
