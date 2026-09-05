"""AUTH-1 · B-011: an API token reaches only what the matrix opens.

The middleware validated the token, stamped its scopes and let the request
through; whether the scope was CHECKED was up to each endpoint, and four of
them did it. A token minted for chat could therefore reach the skills routes,
create resources under the technical owner `api`, start audits and spend the
model budget.

`core/authz.py` is the matrix and it denies by default. These tests pin the
whole reachable surface — so widening it is a deliberate act with a diff, not
something that happens because a new router was mounted.
"""

import pytest

from core.authz import (
    API_TOKEN_RULES,
    KNOWN_SCOPES,
    api_rule_for,
    api_surface,
    api_token_allowed,
)


# ── the surface, pinned ───────────────────────────────────────────────────

def test_the_reachable_surface_is_exactly_this():
    """If this list grows, someone widened what a bearer token can touch."""
    assert api_surface() == {
        "DELETE|PATCH|POST|PUT /api/v1/chat": ("chat",),
        "GET|HEAD|OPTIONS /api/models": ("chat",),
        "DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT /api/codex/*": tuple(sorted(KNOWN_SCOPES)),
        "DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT /api/dispatch*": ("agents:dispatch",),
        "GET|HEAD|OPTIONS /api/changesets/from-dispatch/*": ("agents:dispatch",),
    }


def test_every_scope_named_in_the_matrix_is_a_real_scope():
    for rule in API_TOKEN_RULES:
        for scope in rule.scopes:
            assert scope in KNOWN_SCOPES, f"{rule.path} names an unknown scope: {scope}"


def test_every_rule_declares_an_effect_class_the_audit_recognises():
    for rule in API_TOKEN_RULES:
        assert rule.effect in ("read", "reversible", "external", "admin"), rule
        assert rule.note, f"{rule.path} has no note saying what it opens"


# ── deny by default ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/skills"),
    ("POST", "/api/skills/install"),
    ("GET", "/api/projects"),
    ("POST", "/api/backup/snapshot"),
    ("GET", "/api/settings/schema"),
    ("POST", "/api/agent/run"),
    ("GET", "/api/mcp/servers"),
    ("POST", "/api/research/start"),
    ("GET", "/api/tokens"),
    ("GET", "/api/models/fit"),
])
def test_a_token_with_every_scope_still_cannot_reach_the_rest_of_the_app(method, path):
    allowed, why = api_token_allowed(method, path, sorted(KNOWN_SCOPES))
    assert allowed is False
    assert "not part of the API-token surface" in why


def test_the_refusal_says_what_is_wrong_without_a_lecture():
    allowed, why = api_token_allowed("POST", "/api/v1/chat", ["todos:read"])
    assert allowed is False
    assert why == "API token missing required scope: chat"


# ── what the profiles were minted for still works ─────────────────────────

@pytest.mark.parametrize("method,path,scopes", [
    ("POST", "/api/v1/chat", ["chat"]),
    ("GET", "/api/models", ["chat"]),
    ("POST", "/api/dispatch", ["agents:dispatch"]),
    ("GET", "/api/dispatch", ["agents:dispatch"]),
    ("GET", "/api/dispatch/abc123", ["agents:dispatch"]),
    ("GET", "/api/dispatch/abc123/wait", ["agents:dispatch"]),
    ("POST", "/api/dispatch/abc123/cancel", ["agents:dispatch"]),
    ("GET", "/api/changesets/from-dispatch/job-1", ["agents:dispatch"]),
    ("GET", "/api/codex/todos", ["todos:read"]),
    ("POST", "/api/codex/todos", ["todos:write"]),
    ("GET", "/api/codex/email/list", ["email:read"]),
    ("POST", "/api/codex/memory/add", ["memory:write"]),
    ("GET", "/api/codex/cookbook/tasks", ["cookbook:read"]),
])
def test_the_documented_integrations_are_not_broken(method, path, scopes):
    allowed, why = api_token_allowed(method, path, scopes)
    assert allowed is True, why


def test_a_scopeless_token_reaches_nothing_even_on_the_codex_surface():
    allowed, why = api_token_allowed("GET", "/api/codex/todos", [])
    assert allowed is False
    assert "scope" in why


def test_the_dispatch_prefix_does_not_leak_into_a_neighbour():
    """`/api/dispatcher-x` must not inherit `/api/dispatch`'s rule."""
    assert api_rule_for("GET", "/api/dispatcher-x") is None
    assert api_rule_for("GET", "/api/dispatch") is not None
    assert api_rule_for("GET", "/api/dispatch/1") is not None


# ── the wiring ────────────────────────────────────────────────────────────

def test_the_middleware_asks_the_matrix_before_letting_a_token_through():
    """A unit test of the decision is not a test of the gate.

    This reads app.py and pins the shape of the token branch: the matrix is
    consulted, and the refusal returns before `call_next`. The end-to-end
    proof is a live 403 from a real token against a denied route (recorded in
    FAUSTUS.md); this keeps the wiring from quietly coming undone.
    """
    from pathlib import Path

    app_py = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "from core.authz import api_token_allowed" in app_py
    branch = app_py.split("if matched_id:", 1)[1].split("return await call_next(request)", 1)[0]
    assert "api_token_allowed(" in branch, "the token branch does not consult the matrix"
    assert "status_code=403" in branch, "the matrix's refusal is not returned"
    # …and it happens before the request is handed on.
    assert branch.index("api_token_allowed(") < branch.index("request.state.api_token_scopes")
