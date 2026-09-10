"""Tests for TOOL-01 / TOOL-03(partial): the single tool catalogue
(`src/tool_registry.py`, `routes/tool_registry_routes.py`).

Route tests build a standalone FastAPI app around
`setup_tool_registry_routes()`, the same TestClient-against-a-real-router
pattern `tests/test_contracts_mcp_and_routes.py` uses for the sibling
contracts routes — not a mock of FastAPI, a real app with just this router
mounted.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Import order matters: src.tool_schemas imports back from src.agent_tools,
# so src.agent_tools (and this module, which imports it first) must load
# before anything imports FUNCTION_TOOL_SCHEMAS directly, or Python sees a
# partially-initialized src.tool_schemas mid-circular-import.
from src.tool_registry import (
    ToolRegistry,
    by_name,
    catalog_fingerprint,
    mcp_status_label,
    missing_capability_coverage,
    snapshot,
)
from src.agent_tools import TOOL_TAGS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _native_names():
    names = {(entry.get("function") or {}).get("name") for entry in FUNCTION_TOOL_SCHEMAS}
    names.discard(None)
    return names


# ── ToolRegistry.snapshot(): coverage, uniqueness, determinism (TOOL-01) ───

def test_every_offerable_tool_has_a_descriptor():
    """Every fence tool (TOOL_TAGS, itself the email/desktop union) and every
    native schema name comes back with a descriptor — the gap
    MAPA_REUTILIZACION records for TOOL-01 ("fragmentado, sin clase
    ToolDescriptor unica")."""
    names = {d.name for d in snapshot()}
    assert TOOL_TAGS <= names
    assert _native_names() <= names


def test_no_duplicate_names_in_the_catalogue():
    rows = snapshot()
    names = [d.name for d in rows]
    assert len(names) == len(set(names))
    assert len(rows) > 0


def test_all_builtin_names_have_explicit_capabilities():
    """Nothing snapshot() covers silently fell back to tool_capabilities'
    fail-high "unknown" classification — the same invariant
    tests/test_external_context_tool_gate.py asserts, checked here from this
    module's own vantage point so a future change to tool_registry.py that
    starts covering a name outside that union is caught locally."""
    assert missing_capability_coverage() == frozenset()


def test_readonly_tool_is_safe_to_retry():
    read_file = by_name(snapshot(), "read_file")
    assert read_file is not None
    assert read_file.effect_class == "read"
    assert read_file.idempotency == "safe_read"


def test_write_tool_is_not_assumed_idempotent():
    write_file = by_name(snapshot(), "write_file")
    assert write_file is not None
    assert write_file.effect_class == "write"
    assert write_file.idempotency == "not_supported"


def test_execute_tool_gets_the_live_idle_timeout_setting(monkeypatch):
    import src.tool_registry as registry

    monkeypatch.setattr(registry, "get_setting", lambda key, default: 45)
    bash = by_name(snapshot(), "bash")
    assert bash.timeout_ms == 45_000


def test_fingerprint_is_deterministic_order_independent_and_content_sensitive():
    rows = snapshot()
    fp1 = catalog_fingerprint(rows)
    assert fp1 == catalog_fingerprint(rows)
    assert fp1 == catalog_fingerprint(list(reversed(rows)))
    assert fp1 != catalog_fingerprint(rows[:-1])


def test_owner_scoping_reuses_the_existing_denylist():
    from src.tool_security import blocked_tools_for_owner

    all_names = {d.name for d in snapshot()}
    scoped_rows = snapshot(owner="some-non-admin-user")
    scoped_names = {d.name for d in scoped_rows}
    blocked = blocked_tools_for_owner("some-non-admin-user")
    assert len(scoped_names) < len(all_names)
    assert not (scoped_names & blocked)


def test_tool_registry_namespace_matches_the_module_functions():
    """The lote's own spec names `ToolRegistry.snapshot(...)` — check the
    namespace class actually forwards to the same functions the tests above
    exercise directly, not a second implementation."""
    assert ToolRegistry.snapshot is snapshot
    assert ToolRegistry.by_name is by_name
    assert ToolRegistry.catalog_fingerprint is catalog_fingerprint


# ── MCP integration (TOOL-03 partial) ───────────────────────────────────────

class _FakeMcpManager:
    def __init__(self, tools, statuses):
        self._tools = tools
        self._statuses = statuses

    def get_all_tools(self):
        return self._tools

    def get_server_status(self, server_id):
        return self._statuses.get(server_id, {"status": "disconnected"})


def _mcp_tool(server_id, name, readonly=False):
    return {
        "server_id": server_id,
        "server_name": server_id,
        "name": name,
        "qualified_name": f"mcp__{server_id}__{name}",
        "description": f"{name} on {server_id}",
        "input_schema": {"type": "object"},
        "is_disabled": False,
        "annotations": {"readOnlyHint": readonly},
    }


def test_mcp_tools_join_the_catalogue_with_a_classified_effect():
    manager = _FakeMcpManager(
        tools=[_mcp_tool("srv_ok", "fetch", readonly=True), _mcp_tool("srv_bad", "delete_all")],
        statuses={"srv_ok": {"status": "connected"}, "srv_bad": {"status": "error", "error": "boom"}},
    )
    rows = snapshot(mcp_manager=manager)
    fetch = by_name(rows, "mcp__srv_ok__fetch")
    delete = by_name(rows, "mcp__srv_bad__delete_all")
    assert fetch is not None and fetch.executor == "mcp:srv_ok"
    assert fetch.effect_class == "read" and fetch.idempotency == "safe_read"
    assert delete is not None and delete.effect_class == "write" and delete.idempotency == "not_supported"


def test_mcp_status_label_covers_connected_error_needs_auth_and_degrades_timeout():
    assert mcp_status_label("connected") == "connected"
    assert mcp_status_label("error") == "error"
    assert mcp_status_label("needs_auth") == "needs_auth"
    assert mcp_status_label("timeout") == "degraded"
    assert mcp_status_label("something_future") == "something_future"  # unmapped: passes through


# ── HTTP routes ──────────────────────────────────────────────────────────

def _client(monkeypatch, mcp_manager=None):
    import routes.tool_registry_routes as route_module

    monkeypatch.setattr(route_module, "require_admin", lambda request: None)
    monkeypatch.setattr(route_module, "get_mcp_manager", lambda: mcp_manager)
    app = FastAPI()
    app.include_router(route_module.setup_tool_registry_routes())
    return TestClient(app)


def test_catalog_route_lists_every_tool_with_a_fingerprint(monkeypatch):
    body = _client(monkeypatch).get("/api/tools/catalog").json()
    names = {t["name"] for t in body["tools"]}
    assert "bash" in names and "read_file" in names
    assert body["count"] == len(body["tools"])
    assert body["fingerprint"]


def test_catalog_route_query_filter_matches_name_or_description(monkeypatch):
    # web_fetch's own description names web_search, so both legitimately
    # match a free-text search for it — the filter is over name AND
    # description on purpose (task item 2's `?q=` by "nombre/descripcion").
    body = _client(monkeypatch).get("/api/tools/catalog", params={"q": "web_search"}).json()
    names = {t["name"] for t in body["tools"]}
    assert "web_search" in names
    assert names <= {"web_search", "web_fetch"}


def test_catalog_route_executor_filter(monkeypatch):
    body = _client(monkeypatch).get("/api/tools/catalog", params={"executor": "fence"}).json()
    assert body["count"] > 0
    assert all(t["executor"] == "fence" for t in body["tools"])


def test_catalog_route_entry_has_the_full_input_schema(monkeypatch):
    r = _client(monkeypatch).get("/api/tools/catalog/read_file")
    assert r.status_code == 200
    tool = r.json()["tool"]
    assert tool["name"] == "read_file"
    assert tool["input_schema"]["properties"]["path"]["type"] == "string"


def test_catalog_route_unknown_tool_is_404(monkeypatch):
    r = _client(monkeypatch).get("/api/tools/catalog/definitely_not_a_real_tool")
    assert r.status_code == 404


def test_catalog_route_surfaces_mcp_server_status(monkeypatch):
    manager = _FakeMcpManager(
        tools=[_mcp_tool("srv_ok", "fetch", readonly=True), _mcp_tool("srv_auth", "post_thing")],
        statuses={"srv_ok": {"status": "connected"}, "srv_auth": {"status": "needs_auth"}},
    )
    body = _client(monkeypatch, mcp_manager=manager).get(
        "/api/tools/catalog", params={"executor": "mcp:srv_auth"}
    ).json()
    assert body["count"] == 1
    assert body["tools"][0]["mcp"]["status"] == "needs_auth"


def test_dry_run_reports_errors_and_bounded_repairs(monkeypatch):
    r = _client(monkeypatch).post(
        "/api/tools/catalog/read_file/dry-run",
        json={"arguments": {"path": "x.txt", "limit": "90"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["schema_available"] is True
    assert body["ok"] is False  # the original call had a wrong-shaped 'limit'
    assert any(rep["field"] == "limit" and rep["to"] == 90 for rep in body["repairs"])
    assert body["repaired_arguments"]["limit"] == 90
    assert body["remaining_errors"] == []


def test_dry_run_never_calls_the_real_tool_handler(monkeypatch):
    """Spy on the executor bash actually dispatches through: if dry-run ever
    started calling it, this raises and the test fails."""
    import src.agent_tools as agent_tools

    def _boom(*args, **kwargs):
        raise AssertionError("dry-run must never invoke the real bash handler")

    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, "bash", _boom)
    r = _client(monkeypatch).post(
        "/api/tools/catalog/bash/dry-run", json={"arguments": {"command": "echo hi"}}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dry_run_on_a_fence_only_tool_says_no_schema_to_check(monkeypatch):
    r = _client(monkeypatch).post(
        "/api/tools/catalog/generate_image/dry-run", json={"arguments": {"prompt": "a cat"}}
    )
    assert r.status_code == 200
    assert r.json()["schema_available"] is False


def test_dry_run_rejects_a_non_object_body(monkeypatch):
    r = _client(monkeypatch).post("/api/tools/catalog/bash/dry-run", json={"arguments": "nope"})
    assert r.status_code == 400
