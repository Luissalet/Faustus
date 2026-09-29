"""Teach Mode must not record uncertain tool outcomes as successful examples."""
import importlib

import pytest

from src.agent_tools import ToolBlock
from src import tool_execution


@pytest.mark.parametrize("status,expected", [
    ("partial", False), ("outcome_unknown", False), ("cancelled", False),
    ("failed", False), ("denied", False), ("conflict", False),
    ("succeeded", True), (None, True),
])
@pytest.mark.asyncio
async def test_capture_uses_normalized_outcome(monkeypatch, status, expected):
    from src import settings
    from services import projects
    teaching = importlib.import_module("src.teach_mode.service")
    original_setting = settings.get_setting
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None:
                        True if key == "agent_teach_mode" else original_setting(key, default))
    monkeypatch.setattr(projects, "project_for_session", lambda *args: {})
    recorded = []
    monkeypatch.setattr(teaching, "capture_tool_observation", lambda **kwargs: recorded.append(kwargs))
    raw = {"exit_code": 0, "output": "synthetic result"}
    if status is not None:
        raw["status"] = status
    async def handler(*args, **kwargs):
        return "synthetic", raw
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", handler)
    _, result = await tool_execution.execute_tool_block(
        ToolBlock("write_file", '{"path":"fixture.txt","content":"synthetic"}'),
        session_id="teach-fixture", owner="alice",
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT)
    assert result is raw
    assert len(recorded) == 1 and recorded[0]["success"] is expected
