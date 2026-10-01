"""A server whose security pre-scan found critical issues is not started until
an admin approves it with the override: for a stdio server, starting it is
running its code. Live check 01-10: the quarantine only disabled its tools,
while the process had already been launched at install and on every boot."""
import asyncio

import src.mcp_manager as mm
from src import extension_manifest


def _manager(monkeypatch, held: bool):
    calls = []

    async def fake_stdio(self, server_id, name, command, args, env, inherit_env=True):
        calls.append(server_id)
        return True

    monkeypatch.setattr(extension_manifest, "is_quarantined_for_security", lambda sid: held)
    monkeypatch.setattr(mm.McpManager, "_connect_stdio", fake_stdio)
    return mm.McpManager(), calls


def test_a_server_on_security_hold_is_not_launched(monkeypatch):
    mgr, calls = _manager(monkeypatch, held=True)
    ok = asyncio.run(mgr.connect_server("s1", "scan-demo", "stdio", command="python", args=["server.py"]))
    assert ok is False and calls == []
    status = mgr._connections["s1"]
    assert status["status"] == "error" and status["quarantined"] is True
    assert "not started" in status["error"] and "override" in status["error"]


def test_a_server_without_a_hold_is_launched(monkeypatch):
    mgr, calls = _manager(monkeypatch, held=False)
    assert asyncio.run(mgr.connect_server("s2", "fine", "stdio", command="python", args=["server.py"])) is True
    assert calls == ["s2"]


def test_a_broken_manifest_store_does_not_block_connecting(monkeypatch):
    def boom(sid):
        raise OSError("manifest store unreadable")
    monkeypatch.setattr(extension_manifest, "is_quarantined_for_security", boom)
    assert mm._quarantined_for_security("s3") is False
