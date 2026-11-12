"""PC paths stay stored while connection and health use the Hub link."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import connector_relay, connector_sidecar, connector_status, hub_link, mcp_manager


@pytest.fixture
def linked(monkeypatch, tmp_path):
    token = tmp_path / "hub-token"
    token.write_text("fixture-secret", encoding="utf-8")
    path = tmp_path / "hub-link.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:8811", "token_file": str(token)}), encoding="utf-8")
    monkeypatch.setenv(hub_link.ENV, str(path))
    monkeypatch.setattr(connector_sidecar, "DATA_DIR", str(tmp_path))
    return connector_sidecar.create_connector(preset_id="links", server_id="fixture-server", owner=None,
        values={"LINKS_DIR": "E:/PC/Links"}, app_url="http://127.0.0.1:5181", ui_url="http://127.0.0.1:5181")


@pytest.mark.asyncio
async def test_remote_resolution_preserves_saved_configuration_and_uses_link_token(linked, monkeypatch):
    before = connector_sidecar.list_connectors(redact=False)
    seen = []

    def call(method, path, **kwargs):
        seen.append((method, path, kwargs["where"]))
        return 200, {"id": "links", "app_url": "http://127.0.0.1:5181", "mcp_path": "/api/mcp/apps/links/mcp"}, None

    monkeypatch.setattr(connector_relay.hoard_hub, "_call", call)
    result = await connector_relay.for_server("fixture-server")
    assert result["url"] == "http://127.0.0.1:8811/api/mcp/apps/links/mcp"
    assert result["token"] == "fixture-secret"
    assert seen[0][1] == "/api/mcp/apps/links"
    assert connector_sidecar.list_connectors(redact=False) == before


@pytest.mark.asyncio
async def test_non_family_and_pc_connections_keep_stdio(linked, monkeypatch):
    monkeypatch.setattr(connector_relay.hoard_hub, "_call", lambda *a, **k: (404, {}, None))
    manager = mcp_manager.McpManager()
    local = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_connect_stdio", local)
    assert await manager.connect_server("fixture-server", "Fixture", "stdio", command="node", args=["E:/bridge.js"], env={"URL": "pc"})
    assert local.await_args.args[2:] == ("node", ["E:/bridge.js"], {"URL": "pc"})
    local.reset_mock()
    monkeypatch.delenv(hub_link.ENV)
    assert await manager.connect_server("fixture-server", "Fixture", "stdio", command="node")
    local.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_routes_saved_stdio_through_authenticated_http(linked, monkeypatch):
    monkeypatch.setattr(connector_relay.hoard_hub, "_call", lambda *a, **k: (200, {
        "id": "links", "app_url": "http://127.0.0.1:5181", "mcp_path": "/api/mcp/apps/links/mcp"}, None))
    manager = mcp_manager.McpManager()
    local = AsyncMock(side_effect=AssertionError("Windows child must be started by Hub"))
    monkeypatch.setattr(manager, "_connect_stdio", local)

    async def remote(server_id, name, url, kind, factory):
        assert url == "http://127.0.0.1:8811/api/mcp/apps/links/mcp"
        assert kind == "http"
        manager._connections[server_id] = {"status": "connected", "transport": kind, "url": url}
        return True

    monkeypatch.setattr(manager, "_connect_remote", remote)
    assert await manager.connect_server("fixture-server", "Fixture", "stdio", command="node", args=["E:/bridge.js"])
    assert manager.get_server_status("fixture-server")["hub_app"] == "links"
    local.assert_not_called()


@pytest.mark.asyncio
async def test_status_checks_pc_health_without_validating_windows_files(linked, monkeypatch):
    from src import connectors
    preset = connectors.get_preset("links")
    health = {"reachable": True, "checked_at": "now", "latency_ms": 3, "status_code": 200,
              "body": {"service": "links-hoard"}, "detail": ""}
    monkeypatch.setattr(connector_relay, "describe", AsyncMock(return_value={"id": "links", "app_url": linked["app_url"], "health": health}))
    monkeypatch.setattr(connector_status, "resolve_preset_values", lambda *a: pytest.fail("Windows validation on Spark"))
    status = await connector_status.compute_status(preset, values=linked["values"], is_enabled=True,
        connector_id=linked["id"], manager_status={"status": "connected", "tool_count": 12}, force_check=True)
    assert status["state"] == "available"
    assert status["app"]["reachable"] is True
    connector_relay.describe.reset_mock()
    status = await connector_status.compute_status(preset, values={}, is_enabled=False,
        connector_id=linked["id"], manager_status={}, force_check=True)
    assert status["state"] == "disabled"
    connector_relay.describe.assert_not_called()


@pytest.mark.asyncio
async def test_remote_migration_leaves_pc_rows_and_values_intact(linked, monkeypatch):
    from src import connector_migration
    monkeypatch.setattr(connector_relay, "describe", AsyncMock(return_value={"id": "links"}))
    before = connector_sidecar.list_connectors(redact=False)
    factory = lambda: pytest.fail("remote family configuration must stay stored")
    assert await connector_migration.migrate_connectors(session_factory=factory) == []
    assert connector_sidecar.list_connectors(redact=False) == before


@pytest.mark.asyncio
async def test_explicit_start_runs_through_pc_hub(linked, monkeypatch):
    monkeypatch.setattr(connector_relay, "describe", AsyncMock(return_value={"id": "links"}))
    seen = []

    def post(path, body, **kwargs):
        seen.append((path, body))
        return 200, {"ok": True, "app": "links", "ready": True}

    monkeypatch.setattr(hub_link, "post", post)
    assert (await connector_relay.start(linked))["ready"] is True
    assert seen == [("/api/apps/links/start", {"wait": True})]


@pytest.mark.asyncio
async def test_hub_down_reports_connection_error_without_windows_spawn(linked, monkeypatch):
    monkeypatch.setattr(connector_relay.hoard_hub, "_call", lambda *a, **k: (None, None, None))
    manager = mcp_manager.McpManager()
    local = AsyncMock()
    monkeypatch.setattr(manager, "_connect_stdio", local)
    assert not await manager.connect_server("fixture-server", "Fixture", "stdio", command="node")
    assert "relay unavailable" in manager.get_server_status("fixture-server")["error"]
    local.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_server_uses_exact_manifest_match_without_sidecar(linked, monkeypatch):
    seen = []

    def call(method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return 200, {"app": {"id": "kinect-hoard", "mcp_path": "/api/mcp/apps/kinect-hoard/mcp"}}, None

    monkeypatch.setattr(connector_relay.hoard_hub, "_call", call)
    result = await connector_relay.for_server("legacy-server", "E:/python.exe", ["E:/mcp_server.py"])
    assert result["id"] == "kinect-hoard"
    assert seen == [("POST", "/api/mcp/resolve", {"command": "E:/python.exe", "args": ["E:/mcp_server.py"]})]


@pytest.mark.asyncio
async def test_requested_tool_starts_closed_remote_app_through_link(linked, monkeypatch):
    manager = mcp_manager.McpManager()
    manager._connections["fixture-server"] = {"name": "Links Hoard", "hub_app": "links"}
    first = {"error": "app is closed"}
    second = {"result": "opened"}
    monkeypatch.setattr(manager, "_call_tool_once", AsyncMock(side_effect=[first, second]))
    monkeypatch.setattr(mcp_manager, "_app_closed_result", lambda value: value is first)
    start = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(connector_relay, "start", start)
    result = await manager.call_tool("mcp__fixture-server__list_links", {})
    start.assert_awaited_once_with({"preset_id": "links"})
    assert result["result"] == "opened"
