"""Unsaved audit and degradation are offline advisories, never execution gates."""
import asyncio
import copy
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import degradation as dg
from src import mcp_config_audit as audit


def row(ident="draft", **kw):
    return {"id": ident, "name": ident, "transport": "stdio", "command": "node",
            "args": ["server.js"], "env": {}, **kw}


def test_candidate_replaces_self_and_only_returns_its_findings():
    peers = [row("original"), row("other", env={"API_KEY": "qa-secret-other-123"})]
    before = copy.deepcopy(peers)
    clean = audit.audit_candidate(row("original"), peers)
    assert clean["findings"] == [] and clean["servers"] == 1
    duplicate = audit.audit_candidate(row("new"), peers)
    assert [f["code"] for f in duplicate["findings"]] == ["duplicate"]
    assert all(f["server_id"] == "new" for f in duplicate["findings"])
    assert peers == before
    assert "qa-secret-other-123" not in json.dumps(duplicate)


def test_secret_is_not_repeated_by_shell_details():
    secret = "QA-Secret-Shell-123"
    report = audit.audit_candidate(row(command="powershell", args=["-Command", "serve", "--token", secret]))
    assert "shell_wrapper" in [f["code"] for f in report["findings"]]
    assert secret.lower() not in json.dumps(report).lower()
    assert report["counts"]["high"] >= 1


def test_wrong_json_container_shapes_do_not_crash_the_preview():
    report = audit.audit_candidate(row(args={"not": "a list"}, env=["not a mapping"]))
    assert report["configuration"] == "candidate" and report["findings"] == []


def test_preview_route_uses_the_core_without_writing_or_starting(monkeypatch):
    import routes.mcp.mcp_routes as routes
    peers = [row("saved")]
    monkeypatch.setattr(audit, "load_configured_servers", lambda: copy.deepcopy(peers))
    monkeypatch.setattr(routes, "require_admin", lambda r: None)
    # No manager methods are needed for this offline route.
    app = FastAPI()
    app.include_router(routes.setup_mcp_routes(SimpleNamespace()))
    client = TestClient(app)
    result = client.post("/api/mcp/audit/preview", json=row("new")).json()
    assert result["configuration"] == "candidate"
    assert result["findings"][0]["code"] == "duplicate"
    assert peers == [row("saved")]
    monkeypatch.setattr(routes, "require_admin", lambda r: (_ for _ in ()).throw(HTTPException(403)))
    assert client.post("/api/mcp/audit/preview", json=row()).status_code == 403


def test_degradation_only_reports_high_enabled_and_never_down():
    low = row(command="npx", args=["-y", "pkg"])
    high = row("enabled", env={"API_KEY": "qa-secret-enabled-123"})
    disabled = row("off", is_enabled=False, env={"API_KEY": "qa-secret-off-123"})
    assert dg._mcp_config({"mcp_audit_servers": [low, disabled]})["state"] == "ok"
    result = dg._mcp_config({"mcp_audit_servers": [high, disabled]})
    assert result["state"] == "degraded"
    assert "1 high findings" in result["cause"] and "enabled" in result["cause"]
    assert "qa-secret" not in result["cause"] and "off:" not in result["cause"]
    assert dg._mcp_config({"mcp_audit_servers": [disabled]})["state"] == "unconfigured"


def test_mcp_candidate_has_same_receipt_and_does_not_read_db(monkeypatch):
    import mcp_servers.harness_server as server
    peers, candidate = [row("saved")], row("new")
    monkeypatch.setattr(audit, "load_configured_servers", lambda: (_ for _ in ()).throw(AssertionError("DB read")))
    result = json.loads(asyncio.run(server.call_tool("mcp_config_audit", {"servers": peers, "candidate": candidate}))[0].text)
    assert result == audit.audit_candidate(candidate, peers)
    bad = json.loads(asyncio.run(server.call_tool("mcp_config_audit", {"candidate": "bad"}))[0].text)
    assert "candidate must" in bad["error"]
