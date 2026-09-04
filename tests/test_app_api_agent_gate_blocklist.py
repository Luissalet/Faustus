"""`app_api` must not reach /api/agent-gate.

The agent gate (src/agent_gate.py, routes/agent_gate_routes.py) is what judges
every tool call an external agent makes, one run at a time, and its answer is
binding. `app_api` loops back over HTTP with the internal-tool token, which
skips the auth layer and the approval card — so a model that could POST there
would be asking its own guard what it will be allowed to do and, holding a run
token, answering for itself.

Two other locks already stand in the way: the route refuses the internal-tool
header, and it is `include_in_schema=False` so `app_api`'s `endpoints` listing
never names it. This file pins the third, because the entry costs one line and
the two others are decisions someone could reasonably revisit.
"""

import json

import pytest


def _no_loopback(monkeypatch, why):
    """Any attempt to actually issue the HTTP call is an immediate failure."""
    import httpx

    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(why)

    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedAsyncClient)


def test_the_gate_route_is_on_the_method_path_blocklist():
    from src.tool_implementations import _APP_API_BLOCKLIST_METHOD_PATH

    assert ("POST", "/api/agent-gate") in _APP_API_BLOCKLIST_METHOD_PATH


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/agent-gate",
    # The real shape: the run token is a path segment, and the blocklist
    # matches by prefix precisely so a token cannot walk past it.
    "/api/agent-gate/9f3c1d2e4b5a",
    "api/agent-gate/9f3c1d2e4b5a",          # normalised before the check runs
])
async def test_app_api_refuses_to_post_to_the_gate(monkeypatch, path):
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, f"app_api must block POST {path} before the loopback")

    result = await do_app_api(
        json.dumps({"action": "call", "method": "POST", "path": path,
                    "body": {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}}),
        owner="admin",
    )

    assert result["exit_code"] == 1
    assert "blocked" in result["error"].lower()
    # The generic fallthrough on this branch talks about the cookbook state
    # file; a refusal that names the wrong endpoint teaches the model nothing.
    assert "gate" in result["error"].lower() and "cookbook" not in result["error"].lower()


@pytest.mark.asyncio
async def test_the_gate_is_not_in_the_endpoint_listing(monkeypatch):
    """`endpoints` must not advertise what `call` refuses — offering a route
    and then blocking it is the trap this codebase keeps stamping out."""
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, "the endpoint listing reads the OpenAPI doc, not the network")

    result = await do_app_api(json.dumps({"action": "endpoints", "filter": "agent"}), owner="admin")
    rows = result.get("endpoints") or []
    assert not [r for r in rows if str(r.get("path", "")).startswith("/api/agent-gate")]
