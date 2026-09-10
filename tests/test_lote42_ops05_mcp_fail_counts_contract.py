"""OPS-05 (lote 42): the Safe mode panel (SystemExtras.tsx) now shows WHY a
quarantined MCP server is quarantined (fail count), not just its raw id.
That field (`mcp_fail_counts`) was already returned by `/api/safe-mode/status`
(`src/safe_mode.py::status()`, not owned by this lote) -- it just was not in
the TS `SafeModeStatus` interface or rendered. This locks the HTTP contract
the new UI code reads, using TestClient against the real router (rule 7).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.diagnostics_routes import setup_diagnostics_routes


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("routes.diagnostics_routes.require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None, None))
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    import src.settings as settings_module
    from src import safe_mode
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    safe_mode._active_cache = None
    safe_mode._reason_cache = ""
    yield


def test_status_reports_the_fail_count_for_a_quarantined_server(client):
    from src import safe_mode
    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
        safe_mode.record_mcp_connection("flaky", "error")

    resp = client.get("/api/safe-mode/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "flaky" in body["quarantined_mcp_servers"]
    assert body["mcp_fail_counts"]["flaky"] == safe_mode.MCP_QUARANTINE_THRESHOLD
