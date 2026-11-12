"""New journeys stay on the native Unity tool shared by REST, agents and MCP."""
import asyncio
from src.unity.client import READ_OPERATIONS, UnityClient
from src.unity.tool import UnityTool, execute


def test_playtest_schema_is_available_without_editor(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Discovery must not start/connect to Unity")
    monkeypatch.setattr(UnityClient, "__init__", unexpected)
    result = execute({"action": "schema"})
    for operation in ("build_playtest", "run_playtest", "playtest_status"):
        assert operation in result["operations"]
        assert result["arguments"][operation]
    assert "playtest_status" in READ_OPERATIONS
    assert "run_playtest" not in READ_OPERATIONS


def test_playtest_parameters_reach_existing_client_once(monkeypatch):
    calls = []
    monkeypatch.setattr(UnityClient, "__init__", lambda self, *args, **kwargs: None)
    def operation(self, name, arguments=None):
        calls.append((name, arguments))
        return {"ok": True, "result": {"state": "starting"}}
    monkeypatch.setattr(UnityClient, "operation", operation)
    arguments = {"output": "D:/temporary/recorded-run", "routes": ["arrow35", "wall_parallel"]}
    result = asyncio.run(UnityTool().execute({"action": "operation", "project": "D:/game", "operation": "run_playtest", "arguments": arguments}))
    assert result["exit_code"] == 0
    assert calls == [("run_playtest", arguments)]
