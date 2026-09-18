"""tests/test_google_oauth_client.py — G3.1/G3.2.

Covers:
  - `src/google_oauth_client.py`: stored > env precedence, the secret is
    encrypted at rest (the file never contains the plaintext),
    `parse_client_secret_json` accepting the `web`/`installed` shapes Google
    downloads and rejecting garbage, `redirect_uris` resolution from the
    request/`X-Forwarded-*`/env.
  - `routes/google_oauth_routes.py` (`setup_google_oauth_routes`): GET never
    returns the secret; PUT validates and saves (raw fields or a pasted
    `client_secret_*.json`, with a `warnings` entry when the file's own
    redirect URIs miss ours); DELETE clears the store, not env; `check`
    interprets Google's `invalid_client`/`invalid_grant` correctly, with no
    live network (mocked `httpx.post`); PUT/DELETE/check are `require_human`
    (refuses the internal-tool token), GET is `require_admin`.
  - The calendar OAuth authorize route actually uses `get_client()`: with a
    stored client and no env vars, it redirects to Google instead of 400.

Pulls endpoints directly out of `setup_*_routes()` and calls them — no
ASGI boot, no live network (mirrors `tests/test_google_calendar.py` /
`tests/test_email_oauth.py`).
"""
from __future__ import annotations

import json
import types
import unittest.mock as mock

import pytest

import src.google_oauth_client as goc
import routes.google_oauth_routes as goroutes

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never touch the real data/ directory — every test gets its own."""
    monkeypatch.setattr(goc, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", raising=False)


class _FakeRequest:
    def __init__(self, scheme="http", host="localhost:7000", headers=None, user="admin"):
        h = {"host": host}
        if headers:
            h.update(headers)
        self.headers = h
        self.url = types.SimpleNamespace(scheme=scheme)
        self.state = types.SimpleNamespace(current_user=user)
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=None))
        self.client = None


def _route(method, path):
    router = goroutes.setup_google_oauth_routes()
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"{method} {path} not found")


async def _put(body, request=None):
    endpoint = _route("PUT", "/api/google/oauth-client")

    class _Req(_FakeRequest):
        async def json(self):
            return body

    return await endpoint(request=request or _Req())


# ── src/google_oauth_client.py: precedence, storage, parsing ──────────────

def test_get_client_falls_back_to_env_when_nothing_stored(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    client = goc.get_client()
    assert client == {"client_id": "env-id.apps.googleusercontent.com", "client_secret": "env-secret", "source": "env"}
    assert goc.configured() is True


def test_get_client_returns_nothing_when_unconfigured():
    assert goc.get_client() == {"client_id": None, "client_secret": None, "source": None}
    assert goc.configured() is False


def test_stored_client_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    goc.set_client("stored-id.apps.googleusercontent.com", "stored-secret")

    client = goc.get_client()
    assert client["source"] == "stored"
    assert client["client_id"] == "stored-id.apps.googleusercontent.com"
    assert client["client_secret"] == "stored-secret"


def test_stored_secret_is_encrypted_on_disk():
    goc.set_client("abc-123.apps.googleusercontent.com", "super-secret-value")
    with open(goc._store_path(), "r", encoding="utf-8") as fh:
        raw = fh.read()
    assert "super-secret-value" not in raw
    data = json.loads(raw)
    assert data["client_id"] == "abc-123.apps.googleusercontent.com"
    assert data["client_secret"].startswith("enc:")


def test_clear_client_removes_stored_but_not_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    goc.set_client("stored-id.apps.googleusercontent.com", "stored-secret")
    goc.clear_client()
    client = goc.get_client()
    assert client["source"] == "env"
    assert client["client_id"] == "env-id.apps.googleusercontent.com"
    # Idempotent — clearing an already-clear store doesn't raise.
    goc.clear_client()


def test_parse_client_secret_json_web_shape():
    text = json.dumps({"web": {
        "client_id": "web-id.apps.googleusercontent.com",
        "client_secret": "web-secret",
        "redirect_uris": ["http://localhost:7000/api/calendar/oauth/google/callback"],
    }})
    parsed = goc.parse_client_secret_json(text)
    assert parsed["client_id"] == "web-id.apps.googleusercontent.com"
    assert parsed["client_secret"] == "web-secret"
    assert parsed["redirect_uris"] == ["http://localhost:7000/api/calendar/oauth/google/callback"]


def test_parse_client_secret_json_installed_shape():
    text = json.dumps({"installed": {"client_id": "i.apps.googleusercontent.com", "client_secret": "s"}})
    parsed = goc.parse_client_secret_json(text)
    assert parsed["client_id"] == "i.apps.googleusercontent.com"
    assert parsed["redirect_uris"] == []


@pytest.mark.parametrize("text", ["not json", "[]", "{}", '{"web": {}}', '{"web": {"client_id": "x"}}'])
def test_parse_client_secret_json_rejects_garbage(text):
    with pytest.raises(ValueError):
        goc.parse_client_secret_json(text)


# ── src/google_oauth_client.py: redirect_uris resolution ──────────────────

def test_redirect_uris_inferred_from_request():
    uris = goc.redirect_uris(_FakeRequest(scheme="https", host="faustus.example.ts.net:7443"))
    assert uris["calendar"] == "https://faustus.example.ts.net:7443/api/calendar/oauth/google/callback"
    assert uris["email"] == "https://faustus.example.ts.net:7443/api/email/oauth/google/callback"
    assert uris["origin"] == "https://faustus.example.ts.net:7443"


def test_redirect_uris_prefers_forwarded_headers():
    req = _FakeRequest(scheme="http", host="internal:7000", headers={
        "x-forwarded-proto": "https", "x-forwarded-host": "public.example.com",
    })
    uris = goc.redirect_uris(req)
    assert uris["calendar"] == "https://public.example.com/api/calendar/oauth/google/callback"
    assert uris["origin"] == "https://public.example.com"


def test_redirect_uris_env_overrides_win_verbatim(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://mail.example.com/api/email/oauth/google/callback")
    uris = goc.redirect_uris(_FakeRequest())
    assert uris["email"] == "https://mail.example.com/api/email/oauth/google/callback"
    # No explicit calendar override: derived by swapping the email one's path.
    assert uris["calendar"] == "https://mail.example.com/api/calendar/oauth/google/callback"


def test_redirect_uris_calendar_override_is_independent(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://mail.example.com/api/email/oauth/google/callback")
    monkeypatch.setenv("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", "https://cal.example.com/api/calendar/oauth/google/callback")
    uris = goc.redirect_uris(_FakeRequest())
    assert uris["calendar"] == "https://cal.example.com/api/calendar/oauth/google/callback"


# ── routes/google_oauth_routes.py: GET ─────────────────────────────────────

async def test_get_never_returns_secret(monkeypatch):
    monkeypatch.setattr(goroutes, "require_admin", lambda request: None)
    goc.set_client("visible-id-0123456789.apps.googleusercontent.com", "top-secret-value")
    get_ep = _route("GET", "/api/google/oauth-client")
    result = await get_ep(request=_FakeRequest())
    blob = json.dumps(result)
    assert "top-secret-value" not in blob
    assert result["configured"] is True
    assert result["source"] == "stored"
    assert result["client_id_hint"].startswith("visibl")
    assert "top-secret-value" not in result["client_id_hint"]
    assert result["steps"] == ["project", "enable_calendar_api", "consent_screen", "test_user", "credentials", "paste"]
    assert result["console_url"] == "https://console.cloud.google.com/apis/credentials"
    assert "redirect_uris" in result and "calendar" in result["redirect_uris"] and "email" in result["redirect_uris"]


async def test_get_unconfigured(monkeypatch):
    monkeypatch.setattr(goroutes, "require_admin", lambda request: None)
    get_ep = _route("GET", "/api/google/oauth-client")
    result = await get_ep(request=_FakeRequest())
    assert result["configured"] is False
    assert result["source"] is None
    assert result["client_id_hint"] is None


# ── routes/google_oauth_routes.py: PUT ─────────────────────────────────────

async def test_put_raw_fields_saves_and_returns_status(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    result = await _put({"client_id": "new-id.apps.googleusercontent.com", "client_secret": "s3cr3t"})
    assert result["configured"] is True
    assert result["source"] == "stored"
    assert goc.get_client()["client_secret"] == "s3cr3t"


async def test_put_client_id_without_suffix_is_400(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    with pytest.raises(HTTPException) as exc:
        await _put({"client_id": "not-a-google-id", "client_secret": "s"})
    assert exc.value.status_code == 400


async def test_put_missing_secret_is_400(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    with pytest.raises(HTTPException) as exc:
        await _put({"client_id": "id.apps.googleusercontent.com", "client_secret": ""})
    assert exc.value.status_code == 400


async def test_put_client_secret_json_web_shape(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    body = json.dumps({"web": {
        "client_id": "from-file.apps.googleusercontent.com",
        "client_secret": "file-secret",
        "redirect_uris": [
            "http://localhost:7000/api/calendar/oauth/google/callback",
            "http://localhost:7000/api/email/oauth/google/callback",
        ],
    }})
    result = await _put({"client_secret_json": body})
    assert result["configured"] is True
    assert goc.get_client()["client_id"] == "from-file.apps.googleusercontent.com"
    assert "warnings" not in result


async def test_put_client_secret_json_installed_shape(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    body = json.dumps({"installed": {"client_id": "inst.apps.googleusercontent.com", "client_secret": "s"}})
    result = await _put({"client_secret_json": body})
    assert result["configured"] is True
    assert goc.get_client()["client_id"] == "inst.apps.googleusercontent.com"


async def test_put_invalid_json_is_400(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    with pytest.raises(HTTPException) as exc:
        await _put({"client_secret_json": "not json at all"})
    assert exc.value.status_code == 400


async def test_put_warns_when_file_redirect_uris_miss_ours(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    body = json.dumps({"web": {
        "client_id": "from-file.apps.googleusercontent.com",
        "client_secret": "file-secret",
        "redirect_uris": ["https://someone-elses-app.example.com/callback"],
    }})
    result = await _put({"client_secret_json": body})
    assert result["warnings"]
    assert "redirect URI" in result["warnings"][0]


# ── routes/google_oauth_routes.py: DELETE ──────────────────────────────────

async def test_delete_clears_store_not_env(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    goc.set_client("stored-id.apps.googleusercontent.com", "stored-secret")

    delete_ep = _route("DELETE", "/api/google/oauth-client")
    result = await delete_ep(request=_FakeRequest())
    assert result == {"ok": True}
    client = goc.get_client()
    assert client["source"] == "env"


# ── routes/google_oauth_routes.py: check ───────────────────────────────────

async def test_check_invalid_client_is_not_ok(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    goc.set_client("bad-id.apps.googleusercontent.com", "bad-secret")
    check_ep = _route("POST", "/api/google/oauth-client/check")
    resp = mock.MagicMock()
    resp.json.return_value = {"error": "invalid_client"}
    with mock.patch("httpx.post", return_value=resp) as post:
        result = await check_ep(request=_FakeRequest())
    assert result["ok"] is False
    assert post.call_args.kwargs["data"]["refresh_token"] == "invalid"


async def test_check_invalid_grant_is_ok(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    goc.set_client("good-id.apps.googleusercontent.com", "good-secret")
    check_ep = _route("POST", "/api/google/oauth-client/check")
    resp = mock.MagicMock()
    resp.json.return_value = {"error": "invalid_grant"}
    with mock.patch("httpx.post", return_value=resp):
        result = await check_ep(request=_FakeRequest())
    assert result["ok"] is True


async def test_check_never_returns_secret_in_detail(monkeypatch):
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    goc.set_client("good-id.apps.googleusercontent.com", "top-secret-check")
    check_ep = _route("POST", "/api/google/oauth-client/check")
    resp = mock.MagicMock()
    resp.json.return_value = {"error": "invalid_grant"}
    with mock.patch("httpx.post", return_value=resp):
        result = await check_ep(request=_FakeRequest())
    assert "top-secret-check" not in json.dumps(result)


async def test_check_unconfigured_is_400(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(goroutes, "require_human", lambda request: None)
    check_ep = _route("POST", "/api/google/oauth-client/check")
    with pytest.raises(HTTPException) as exc:
        await check_ep(request=_FakeRequest())
    assert exc.value.status_code == 400


# ── require_human on PUT/DELETE/check; require_admin on GET ────────────────

def _internal_tool_request():
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    return _FakeRequest(headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})


async def test_put_refuses_the_internal_tool_token():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await _put({"client_id": "x.apps.googleusercontent.com", "client_secret": "s"}, request=_internal_tool_request())
    assert exc.value.status_code == 403


async def test_delete_refuses_the_internal_tool_token():
    from fastapi import HTTPException
    delete_ep = _route("DELETE", "/api/google/oauth-client")
    with pytest.raises(HTTPException) as exc:
        await delete_ep(request=_internal_tool_request())
    assert exc.value.status_code == 403


async def test_check_refuses_the_internal_tool_token():
    from fastapi import HTTPException
    check_ep = _route("POST", "/api/google/oauth-client/check")
    with pytest.raises(HTTPException) as exc:
        await check_ep(request=_internal_tool_request())
    assert exc.value.status_code == 403


async def test_get_allows_the_internal_tool_token():
    """`require_admin` (unlike `require_human`) accepts the loopback
    in-process token by design — read-only status is fine for a tool call."""
    get_ep = _route("GET", "/api/google/oauth-client")
    result = await get_ep(request=_internal_tool_request())
    assert result["configured"] is False


# ── calendar/email routes actually use get_client() ─────────────────────

async def test_calendar_authorize_uses_stored_client_with_no_env():
    """With a stored client and no env vars at all, the calendar authorize
    route redirects to Google instead of 400 — proof it went through
    `get_client()`, not a direct `os.environ.get`."""
    import routes.calendar_routes as croutes

    goc.set_client("stored-cal.apps.googleusercontent.com", "stored-cal-secret")
    router = croutes.setup_calendar_routes()
    authorize = None
    for r in router.routes:
        if r.path.endswith("/oauth/google/authorize") and "GET" in getattr(r, "methods", set()):
            authorize = r.endpoint
    assert authorize is not None

    class _Req(_FakeRequest):
        pass

    resp = await authorize(request=_Req(host="localhost:7000"), account_id="")
    assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "stored-cal.apps.googleusercontent.com" in resp.headers["location"]


async def test_email_authorize_uses_stored_client_with_no_env():
    import routes.email_routes as eroutes

    goc.set_client("stored-mail.apps.googleusercontent.com", "stored-mail-secret")
    router = eroutes.setup_email_routes()
    authorize = None
    for r in router.routes:
        if r.path == "/api/email/oauth/google/authorize" and "GET" in getattr(r, "methods", set()):
            authorize = r.endpoint
    assert authorize is not None

    resp = await authorize(account_id="acct-x", request=_FakeRequest(), owner="")
    assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "stored-mail.apps.googleusercontent.com" in resp.headers["location"]
