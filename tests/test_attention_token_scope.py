"""A read-only token scope for GET /api/attention (radar #403)."""
import ast
from pathlib import Path

import pytest

from core.authz import KNOWN_SCOPES, api_token_allowed


def test_scope_is_known_and_mintable():
    assert "attention:read" in KNOWN_SCOPES
    src = Path(__file__).resolve().parents[1] / "routes" / "api_token_routes.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    allowed = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ALLOWED_SCOPES" for t in node.targets):
            allowed = {elt.value for elt in node.value.elts}
    assert allowed is not None and "attention:read" in allowed


@pytest.mark.parametrize("scopes", [["attention:read"], ["sessions"]])
def test_attention_read_is_open_to_the_scope(scopes):
    allowed, why = api_token_allowed("GET", "/api/attention", scopes)
    assert allowed is True, why


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/attention/read"),
    ("GET", "/api/sessions"),
    ("POST", "/api/session"),
    ("GET", "/api/approvals"),
])
def test_attention_scope_opens_nothing_else(method, path):
    assert api_token_allowed(method, path, ["attention:read"])[0] is False


def test_other_scopes_do_not_open_attention():
    assert api_token_allowed("GET", "/api/attention", ["chat"])[0] is False
    assert api_token_allowed("GET", "/api/attention", [])[0] is False