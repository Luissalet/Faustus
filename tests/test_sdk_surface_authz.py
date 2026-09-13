"""S1.1: the `sessions` scope — the surface a paired external SDK client
drives a chat session through end to end (create -> stream a turn over SSE,
including an in-turn tool-approval answer -> stop/pause/steer -> resume ->
read history/artifacts), without a cookie.

Three things this file pins, matching CONTRATO_SDK_S1.md's own "Tests"
section:

1. The matrix (`core/authz.py`): every route S1.1 opens is reachable with
   the `sessions` scope and refused (with an honest reason) without it;
   `chat` alone does not open `/api/chat_stream`; a route NOT in this list
   stays exactly as closed as it was before this lot ("not part of the
   API-token surface").
2. Ownership (`src/auth_helpers.effective_user`): data a token touches is
   attributed to the TOKEN'S OWNER, never the "api" sandboxed pseudo-user —
   a session created with token A is visible under A's owner's cookie, a
   different owner's cookie 404s it, and a token missing the `sessions`
   scope never reaches the route at all.
3. `require_human` does not gate `POST /api/chat_stream` (an API token is
   not "internal-tool", so it must be able to answer an in-turn
   `tool_approval` the same way a cookie session does), and a real turn
   answering one via a bearer token is accepted end to end.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from core.authz import KNOWN_SCOPES, api_token_allowed

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── the exact surface S1.1 opens (core/authz.py) ────────────────────────────
# Kept here as a second, independent listing (not imported from core/authz.py)
# so a change to the matrix has to be deliberately mirrored in the test that
# pins it, the same discipline tests/test_auth1_token_matrix.py already
# applies to the rest of the surface.
SESSIONS_ROUTES = [
    ("POST", "/api/session"),
    ("GET", "/api/sessions"),
    ("PATCH", "/api/session/{sid}"),
    ("DELETE", "/api/session/{sid}"),
    ("GET", "/api/session/{sid}/connectors"),
    ("PATCH", "/api/session/{sid}/connectors"),
    ("GET", "/api/session/{sid}/tool-support"),
    ("GET", "/api/session/{session_id}/context_info"),
    ("GET", "/api/history/{session_id}"),
    ("POST", "/api/chat_stream"),
    ("GET", "/api/chat/resume/{session_id}"),
    ("GET", "/api/chat/stream_status/{session_id}"),
    ("GET", "/api/chat/activity"),
    ("POST", "/api/chat/stop/{session_id}"),
    ("POST", "/api/chat/pause/{session_id}"),
    ("POST", "/api/chat/steer/{session_id}"),
    ("GET", "/api/questions"),
    ("GET", "/api/approvals/pending"),
    ("GET", "/api/approvals/active"),
    ("GET", "/api/artifacts"),
    ("GET", "/api/artifacts/{artifact_id}"),
    ("GET", "/api/artifacts/{artifact_id}/download"),
    ("GET", "/api/artifacts/{artifact_id}/manifest"),
    ("GET", "/api/artifacts/{artifact_id}/provenance"),
    ("GET", "/openapi.json"),
]

#: routes a paired SDK client explicitly does NOT get in this lot — grant/
#: deny/revoke stay `require_human` (a person decides, never a token), and
#: everything else here (settings, models, projects, token/session
#: management itself, mutating an artifact) was never part of the surface.
STILL_CLOSED_ROUTES = [
    ("POST", "/api/approvals/a1/grant"),
    ("POST", "/api/approvals/a1/deny"),
    ("DELETE", "/api/approvals/a1"),
    ("POST", "/api/approvals/request"),
    ("POST", "/api/approvals/check"),
    ("POST", "/api/artifacts/a1/review"),
    ("DELETE", "/api/artifacts/a1"),
    ("GET", "/api/session/s1"),  # never existed; the SDK uses GET /api/sessions
    ("GET", "/api/tokens"),
    ("GET", "/api/settings/schema"),
    ("GET", "/api/models/fit"),
    ("GET", "/api/projects"),
]


def _concrete(path: str) -> str:
    """Substitute every `{param}` with a concrete segment, same shape
    `Rule.matches` expects a real request path to have."""
    return re.sub(r"\{[^}]+\}", "x1", path)


@pytest.mark.parametrize("method,path", SESSIONS_ROUTES)
def test_sessions_scope_opens_every_route_of_the_surface(method, path):
    allowed, why = api_token_allowed(method, _concrete(path), ["sessions"])
    assert allowed is True, why


@pytest.mark.parametrize("method,path", SESSIONS_ROUTES)
def test_missing_sessions_scope_is_refused_with_the_scope_named(method, path):
    allowed, why = api_token_allowed(method, _concrete(path), ["chat"])
    assert allowed is False
    assert "sessions" in why


@pytest.mark.parametrize("method,path", SESSIONS_ROUTES)
def test_a_scopeless_token_is_refused_too(method, path):
    allowed, _why = api_token_allowed(method, _concrete(path), [])
    assert allowed is False


def test_chat_scope_alone_does_not_open_chat_stream():
    """`chat` opens the older, non-streaming `/api/v1/chat` — not the SDK's
    streaming turn route, which needs `sessions`."""
    allowed, why = api_token_allowed("POST", "/api/chat_stream", ["chat"])
    assert allowed is False
    assert "sessions" in why


def test_sessions_scope_alone_does_not_open_the_legacy_chat_route():
    """And the reverse: `sessions` does not silently inherit `/api/v1/chat`."""
    allowed, _why = api_token_allowed("POST", "/api/v1/chat", ["sessions"])
    assert allowed is False


@pytest.mark.parametrize("method,path", STILL_CLOSED_ROUTES)
def test_routes_outside_the_surface_stay_closed(method, path):
    allowed, why = api_token_allowed(method, path, sorted(KNOWN_SCOPES))
    assert allowed is False
    assert "not part of the API-token surface" in why


def test_the_denial_reason_names_the_session_surface():
    """S1.1: the 403 for an unlisted route stayed true after this lot opened
    the session surface — it must say so, not just "chat, codex-skill and
    dispatch" as it did before."""
    _allowed, why = api_token_allowed("GET", "/api/tokens", sorted(KNOWN_SCOPES))
    assert "session" in why


def test_sessions_is_a_known_scope():
    assert "sessions" in KNOWN_SCOPES


def test_sdk_profile_is_chat_plus_sessions():
    from routes.api_token_routes import ALLOWED_SCOPES, TOKEN_PROFILES

    assert TOKEN_PROFILES["sdk"] == ["chat", "sessions"]
    assert "sessions" in ALLOWED_SCOPES


# ── ownership: a token's data is the TOKEN OWNER'S, never "api" ─────────────
#
# Direct-handler extraction against the REAL route functions, the pattern
# tests/test_session_delete_stops_run.py and tests/test_api_token_routes.py
# already use for this router: setup_session_routes(...) builds a fresh
# router closed over a fake manager, no TestClient/ASGI transport needed to
# exercise the real `create_session`/`_verify_session_owner` bodies.


def _stub_multipart_if_missing(monkeypatch):
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


def _route(router, path, method="GET"):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _token_request(owner: str, scopes=("chat", "sessions")):
    """What the auth middleware stamps on request.state for a bearer `ody_`
    token (app.py's AuthMiddleware, `if matched_id:` branch): current_user
    is always the sandboxed pseudo-user "api", the real owner lives in
    api_token_owner."""
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        state=SimpleNamespace(
            current_user="api",
            api_token=True,
            api_token_id="tok1",
            api_token_owner=owner,
            api_token_scopes=list(scopes),
        ),
    )


def _cookie_request(user: str):
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        state=SimpleNamespace(current_user=user, api_token=False),
    )


def test_a_session_created_with_a_token_is_owned_by_the_token_owner(monkeypatch):
    """POST /api/session with a bearer token must stamp the session's owner
    as api_token_owner — not "api", the middleware's sandboxed identity for
    every token request (src/auth_helpers.py's effective_user is exactly
    the seam that makes this a one-line difference from a cookie call)."""
    _stub_multipart_if_missing(monkeypatch)
    import routes.session_routes as sr
    import src.event_bus as event_bus

    monkeypatch.setattr(event_bus, "fire_event", lambda *a, **kw: None)

    session_manager = MagicMock()
    session_manager.create_session.return_value = SimpleNamespace(name="", headers=None)

    router = sr.setup_session_routes(session_manager, {})
    create_session = _route(router, "/api/session", "POST")

    request = _token_request(owner="alice")
    create_session(
        request=request,
        name="from-the-sdk",
        endpoint_url="",
        model="",
        rag=None,
        skip_validation="true",
        api_key="",
        endpoint_id="",
    )

    assert session_manager.create_session.call_args.kwargs["owner"] == "alice"


def test_a_scopeless_or_wrong_scope_token_never_reaches_create_session():
    """The route body above is unreachable at all without `sessions` — the
    middleware's matrix check runs first (app.py, before request.state.*
    is even stamped). Documented here as the other half of the ownership
    story: propiedad only matters for a request the matrix let through."""
    allowed, why = api_token_allowed("POST", "/api/session", ["chat"])
    assert allowed is False
    assert why == "API token missing required scope: sessions"


def test_owned_session_is_visible_under_the_owners_own_cookie_not_another_users(monkeypatch):
    _stub_multipart_if_missing(monkeypatch)
    import routes.session_routes as sr

    class _Row:
        owner = "alice"

    class _Query:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return _Row()

    class _Db:
        def query(self, *a, **kw):
            return _Query()

        def close(self):
            return None

    monkeypatch.setattr(sr, "SessionLocal", lambda: _Db())

    # The owner's own cookie: no exception.
    sr._verify_session_owner(_cookie_request("alice"), "sess-1")

    # A different signed-in user: 404, not 403 — existence is not leaked.
    with pytest.raises(HTTPException) as exc:
        sr._verify_session_owner(_cookie_request("bob"), "sess-1")
    assert exc.value.status_code == 404


def test_a_different_owners_token_gets_404_on_the_same_session(monkeypatch):
    """A second token, minted for a different owner, must not reach a
    session token A created — same 404 a foreign cookie session gets, via
    the same effective_user() seam."""
    _stub_multipart_if_missing(monkeypatch)
    import routes.session_routes as sr

    class _Row:
        owner = "alice"

    class _Query:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return _Row()

    class _Db:
        def query(self, *a, **kw):
            return _Query()

        def close(self):
            return None

    monkeypatch.setattr(sr, "SessionLocal", lambda: _Db())

    with pytest.raises(HTTPException) as exc:
        sr._verify_session_owner(_token_request(owner="mallory"), "sess-1")
    assert exc.value.status_code == 404


# ── require_human does not gate chat_stream; a token can answer an
#    in-turn tool_approval end to end ───────────────────────────────────────


def test_chat_routes_never_imports_require_human():
    """A source-level guard, the same shape as
    test_auth1_token_matrix.py::test_the_middleware_asks_the_matrix_before_...:
    require_human refuses ANY api_token request outright (it exists to keep
    even Faustus's own in-process tool loopback out of granting its own
    approvals) — chat_stream and the tool_approval_decision it accepts must
    never call it, or a `sessions`-scoped token could never answer an
    in-turn approval at all."""
    source = (REPO_ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    assert "require_human" not in source


@pytest.mark.asyncio
async def test_a_token_can_answer_an_in_turn_tool_approval(monkeypatch):
    """End to end against the real `POST /api/chat_stream` route function
    (reusing tests/test_foreground_model_routing.py's harness, which fakes
    only the model call and session plumbing — src.tool_approvals and the
    route's own ownership check run for real), with the request carrying
    bearer-token state instead of a cookie. `effective_user` is put back to
    the REAL implementation (the harness pins it to a hardcoded "alice" by
    default) so the token's owner is resolved exactly the way app.py's
    middleware would hand it off — api_token_owner, not the "api" pseudo-user."""
    from src.auth_helpers import effective_user as real_effective_user
    from src.tool_capabilities import capabilities_for_action
    from test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    import routes.chat_routes as chat_routes

    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    # The harness hardcodes effective_user -> "alice" for simplicity; restore
    # the real seam so this test actually exercises S1.1's ownership fix.
    monkeypatch.setattr(chat_routes, "effective_user", real_effective_user)

    pending = chat_routes.tool_approval_store.create(
        owner="alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content="printf hi",
        workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf hi"),
    )

    request = _RouteRequest("agent")
    request.state = _token_request(owner="alice").state
    request._form.update({
        "tool_approval_id": pending.approval_id,
        "tool_approval_decision": "approve",
        "compare_mode": "false",
    })

    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass

    assert captured["exact_approval"].pending == pending
    assert chat_routes.tool_approval_store.peek(pending.approval_id) is None


# ── GET /api/approvals/pending and /active: a token reads its OWN owner
#    only, never an arbitrary owner= it might pass ─────────────────────────


def test_a_token_reading_approvals_ignores_a_foreign_owner_query_param(monkeypatch):
    """require_admin would 403 any bearer token outright (see
    routes/approvals_routes.py::_read_owner) — S1.1 replaces it, for tokens
    only, with the token's own scope+owner, and pins the read to that owner
    regardless of what `owner=` the caller asked for."""
    import routes.approvals_routes as ar

    seen = {}
    monkeypatch.setattr(ar.approval_store, "expire_stale", lambda: None)
    def _fake_pending(owner="", limit=50):
        seen["pending_owner"] = owner
        return []

    def _fake_active(owner="", limit=50):
        seen["active_owner"] = owner
        return []

    monkeypatch.setattr(ar.approval_store, "pending", _fake_pending)
    monkeypatch.setattr(ar.approval_store, "active", _fake_active)

    router = ar.setup_approvals_routes()
    list_pending = _route(router, "/api/approvals/pending", "GET")
    list_active = _route(router, "/api/approvals/active", "GET")

    request = _token_request(owner="alice")
    list_pending(request=request, owner="somebody-else", limit=50)
    list_active(request=request, owner="somebody-else", limit=50)

    assert seen["pending_owner"] == "alice"
    assert seen["active_owner"] == "alice"


def test_a_token_without_sessions_scope_cannot_read_approvals():
    import routes.approvals_routes as ar

    router = ar.setup_approvals_routes()
    list_pending = _route(router, "/api/approvals/pending", "GET")

    request = _token_request(owner="alice", scopes=("chat",))
    with pytest.raises(HTTPException) as exc:
        list_pending(request=request, owner="", limit=50)
    assert exc.value.status_code == 403
