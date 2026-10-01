"""An admin's approval of a server held by the security pre-scan lasts.

Live (01-10): after approving, the server was scanned again on its next
connection, the same critical finding held it again, and the approval did
not survive a restart. The approval now remembers the critical findings it
accepted; the same ones stay approved, a new one holds the server again.
"""
from __future__ import annotations

import pytest

import routes.mcp.mcp_routes as mr
from src import extension_manifest as em
from src import settings as settings_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(em, "_quarantine", lambda sid: None)
    monkeypatch.setattr(em, "_unquarantine", lambda sid: None)
    yield
    settings_mod._invalidate_caches()


def _scan(snippet="tok = _token()", severity="critical"):
    return {"findings": [
        {"rule_id": "EXFIL_SECRET_TO_NETWORK", "severity": severity, "file": "app/family.py", "line": 86, "snippet": snippet},
        {"rule_id": "CRED_ENV_SECRET_DUMP", "severity": "medium", "file": "app/server.py", "line": 4, "snippet": "os.environ.get('X')"},
    ], "counts": {severity: 1}}


def test_the_fingerprint_is_the_set_of_critical_findings():
    assert em.critical_findings_fingerprint(_scan()) == em.critical_findings_fingerprint(_scan())
    assert em.critical_findings_fingerprint(_scan()) != em.critical_findings_fingerprint(_scan("tok = other()"))
    assert em.critical_findings_fingerprint(_scan(severity="medium")) == ""
    assert em.critical_findings_fingerprint(None) == ""


def test_an_approval_covers_the_same_findings_and_not_new_ones(store):
    em.record_install_or_update("s1", name="Hoard", command="python", args=["server.py"], permissions={"network": True})
    em.attach_security_scan("s1", _scan())
    em.quarantine_for_security("s1")
    assert em.is_quarantined_for_security("s1")
    assert not em.security_scan_already_approved("s1", _scan())

    em.approve_security_scan("s1")
    assert not em.is_quarantined_for_security("s1")
    assert em.security_scan_already_approved("s1", _scan())
    assert not em.security_scan_already_approved("s1", _scan("tok = read_all_keys()"))

    # an update of the server keeps what was approved
    em.record_install_or_update("s1", name="Hoard", version="2", command="python", args=["server.py"], permissions={"network": True})
    assert em.security_scan_already_approved("s1", _scan())


class _Mgr:
    def get_all_tools(self):
        return [{"server_id": "s1", "name": "family_call", "description": "Call another app."}]


def test_the_next_connection_does_not_hold_an_approved_server_again(store, monkeypatch):
    em.record_install_or_update("s1", name="Hoard", command="python", args=["server.py"], permissions={"network": True})
    em.attach_security_scan("s1", _scan())
    em.quarantine_for_security("s1")
    em.approve_security_scan("s1")

    class _Result:
        def __init__(self, d):
            self.d = d

        def to_dict(self):
            return self.d

        def has_critical(self):
            return bool(em.critical_findings_fingerprint(self.d))

    monkeypatch.setattr(mr, "scan_mcp_server_config", lambda **kw: _Result(_scan()))
    out = mr.scan_connected_tools(_Mgr(), "s1", name="Hoard", transport="stdio", command="python", args=["server.py"], env={})
    assert out["security_scan_quarantined"] is False and not em.is_quarantined_for_security("s1")

    monkeypatch.setattr(mr, "scan_mcp_server_config", lambda **kw: _Result(_scan("tok = read_all_keys()")))
    out = mr.scan_connected_tools(_Mgr(), "s1", name="Hoard", transport="stdio", command="python", args=["server.py"], env={})
    assert out["security_scan_quarantined"] is True and em.is_quarantined_for_security("s1")


def _connect(monkeypatch, scan_dict):
    import asyncio
    import src.mcp_manager as mm

    calls = []

    async def fake_stdio(self, server_id, name, command, args, env, inherit_env=True):
        calls.append(server_id)
        return True

    class _Result:
        def to_dict(self):
            return scan_dict

        def has_critical(self):
            return bool(em.critical_findings_fingerprint(scan_dict))

    monkeypatch.setattr(mm.McpManager, "_connect_stdio", fake_stdio)
    monkeypatch.setattr(mr, "scan_mcp_server_config", lambda **kw: _Result())
    mgr = mm.McpManager()
    ok = asyncio.run(mgr.connect_server("s1", "Hoard", "stdio", command="python", args=["server.py"]))
    return ok, calls


def _held(store_scan):
    em.record_install_or_update("s1", name="Hoard", command="python", args=["server.py"], permissions={"network": True})
    em.attach_security_scan("s1", store_scan)
    em.quarantine_for_security("s1")


def test_a_hold_with_nothing_critical_left_is_lifted_at_start(store, monkeypatch):
    _held(_scan())
    ok, calls = _connect(monkeypatch, _scan(severity="medium"))
    assert ok is True and calls == ["s1"] and not em.is_quarantined_for_security("s1")


def test_a_hold_whose_finding_is_still_there_stays(store, monkeypatch):
    _held(_scan())
    ok, calls = _connect(monkeypatch, _scan())
    assert ok is False and calls == [] and em.is_quarantined_for_security("s1")
