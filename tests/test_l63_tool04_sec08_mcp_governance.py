"""L63 · TOOL-04 + SEC-08 — the automatic install/update hook in
routes/mcp/mcp_routes.py (docs/spec/v2/backlog.json TOOL-04, SEC-08).

Before this lote `extension_manifest.record_install_or_update` and
`security_policy.evaluate`/`ConsentStore` both existed but nothing in the
REAL create/update flow (`POST /servers`, `PATCH /servers/{id}/env-mode`)
ever called them — a manifest only got written through the separate manual
`POST /servers/{id}/manifest` route (`tests/test_l43_tool04_manifest_routes.py`,
which keeps passing unmodified — rule 3). This drives the real router
handlers directly (the pattern `tests/test_mcp_routes_env_mode.py`'s own
docstring documents: these async handlers do not survive a `TestClient`
portal reliably under this repo's pytest-asyncio auto mode) with a fake
`McpManager` that only records what it was asked, so no real subprocess is
spawned.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import routes.mcp.mcp_routes as mcp_routes
import src.settings as settings_mod
from core.database import McpServer
from src import extension_manifest, security_policy
from src.mcp_manager import McpManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_routes, "SessionLocal", Session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def manager(monkeypatch):
    """A McpManager whose connect/disconnect only record what they were asked."""
    mgr = McpManager()
    connected = {}

    async def fake_connect(server_id, name, transport, command=None, args=None,
                           env=None, url=None, inherit_env=None):
        connected[server_id] = {"inherit_env": inherit_env, "env": env}
        mgr._connections[server_id] = {
            "status": "connected", "name": name, "transport": transport,
            "tool_count": 0, "inherit_env": True if inherit_env is None else bool(inherit_env),
        }
        return True

    async def fake_disconnect(server_id):
        mgr._connections.pop(server_id, None)

    monkeypatch.setattr(mgr, "connect_server", fake_connect)
    monkeypatch.setattr(mgr, "disconnect_server", fake_disconnect)
    mgr.connected = connected
    return mgr


@pytest.fixture
def routes(monkeypatch, manager, tmp_path):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    router = mcp_routes.setup_mcp_routes(manager)
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint
    yield by_path
    settings_mod._invalidate_caches()


@pytest.fixture(autouse=True)
def isolated_governance(tmp_path, monkeypatch):
    """Neither the manifest settings store nor the SEC-08 audit/consent
    singletons may leak between tests or write into the real repo: the
    manifest lives under the same monkeypatched `settings_mod.SETTINGS_FILE`
    as `routes` above, and the audit trail is pointed at a throwaway dir."""
    monkeypatch.setattr(security_policy, "AUDIT_DATA_DIR", str(tmp_path / "sec08"))
    security_policy.consent_store._records.clear()
    security_policy.clear_audit_log()
    yield
    security_policy.consent_store._records.clear()
    security_policy.clear_audit_log()


REQ = object()


def _call(fn, **kwargs):
    result = fn(request=REQ, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _add_server(routes, *, declared_permissions=None, command="npx", inherit_env="false"):
    return _call(
        routes[("POST", "/api/mcp/servers")],
        name="Some MCP", transport="stdio", command=command,
        args=json.dumps(["-y", "some-server"]), env="{}", url=None,
        oauth_file=None, oauth_config=None, inherit_env=inherit_env,
        declared_permissions=json.dumps(declared_permissions) if declared_permissions else None,
    )


# ── install: the automatic hook records a manifest and evaluates it ────────

def test_install_with_declared_permissions_records_the_manifest_and_diff(routes, db):
    body = _add_server(routes, declared_permissions={"network": True})
    assert body["declared_permissions"] == {"network": True, "files": False, "secrets": False}
    assert body["manifest_diff"]["added"] == ["network"]
    assert body["manifest_quarantined"] is False
    assert body["policy_decision"]["allowed"] is True

    manifest = extension_manifest.get_manifest(body["id"])
    assert manifest is not None
    assert manifest["permissions"]["network"] is True


def test_install_without_declared_permissions_falls_back_to_suggested(routes, db):
    """`command="npx"` is one of `suggested_permissions`'s own network markers."""
    body = _add_server(routes, declared_permissions=None, command="npx")
    assert body["declared_permissions"]["network"] is True


def test_a_fresh_install_is_never_quarantined(routes, db):
    body = _add_server(routes, declared_permissions={"network": True, "files": True, "secrets": True})
    assert body["manifest_quarantined"] is False
    assert extension_manifest.is_quarantined_for_permissions(body["id"]) is False


def test_inheriting_the_full_environment_at_install_forces_the_secrets_permission(routes, db):
    body = _add_server(routes, declared_permissions={"network": False}, inherit_env="true")
    assert body["declared_permissions"]["secrets"] is True


# ── update: escalating a permission quarantines and invalidates consent ────

def test_escalating_env_mode_to_inherited_is_flagged_as_added_and_quarantined(routes, db, manager):
    installed = _add_server(routes, declared_permissions={"network": False}, inherit_env="false")
    server_id = installed["id"]
    assert installed["declared_permissions"]["secrets"] is False

    updated = _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
                    server_id=server_id, inherit_env="true")
    assert updated["manifest_diff"]["added"] == ["secrets"]
    assert updated["manifest_quarantined"] is True
    assert extension_manifest.is_quarantined_for_permissions(server_id) is True
    # SEC-08: the escalation is refused by policy until explicitly approved —
    # prior consent (for {} / no secrets) does not cover the new effect.
    assert updated["policy_decision"]["allowed"] is False
    assert updated["policy_decision"]["requires_approval"] is True


def test_an_update_that_adds_nothing_stays_consented_and_unquarantined(routes, db, manager):
    installed = _add_server(routes, declared_permissions={"network": True}, inherit_env="false")
    server_id = installed["id"]
    # Toggling env-mode to the SAME (still minimal) value declares nothing new.
    updated = _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
                    server_id=server_id, inherit_env="false")
    assert updated["manifest_quarantined"] is False
    assert updated["policy_decision"]["allowed"] is True


def test_approving_an_escalation_restores_a_consented_decision(routes, db, manager):
    installed = _add_server(routes, declared_permissions={"network": False}, inherit_env="false")
    server_id = installed["id"]
    _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
         server_id=server_id, inherit_env="true")
    assert extension_manifest.is_quarantined_for_permissions(server_id) is True

    approve = _call(routes[("POST", "/api/mcp/servers/{server_id}/manifest/approve")], server_id=server_id)
    assert approve["manifest"]["pending_approval"] is False
    assert extension_manifest.is_quarantined_for_permissions(server_id) is False

    effects = security_policy.effects_for_declared_permissions(approve["manifest"]["permissions"])
    decision = security_policy.evaluate(
        list(effects), plugin_id=server_id, tool_name="*", consents=security_policy.consent_store)
    assert decision.allowed is True


def test_escalating_one_server_never_touches_another_servers_consent(routes, db, manager):
    """The SEC-08 acceptance case, end to end: an update on one server must
    not block a sibling server's tools."""
    other = _add_server(routes, declared_permissions={"network": True}, inherit_env="false")
    target = _add_server(routes, declared_permissions={"network": False}, inherit_env="false")

    _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
         server_id=target["id"], inherit_env="true")

    other_effects = security_policy.effects_for_declared_permissions({"network": True})
    other_decision = security_policy.evaluate(
        list(other_effects), plugin_id=other["id"], tool_name="*", consents=security_policy.consent_store)
    assert other_decision.allowed is True


# ── the audit trail persists to disk (SEC-08) ───────────────────────────────

def test_every_governance_decision_is_persisted_to_disk(routes, db, tmp_path):
    _add_server(routes, declared_permissions={"network": True})
    path = security_policy.audit_log_path()
    assert path.startswith(str(tmp_path))
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert lines
    record = json.loads(lines[-1])
    assert "reason" in record and "allowed" in record
