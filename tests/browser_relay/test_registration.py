"""The real-session backend uses the existing builtin selection/lifecycle."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("selection,expected", [("browser_session", True), ("none", False)])
async def test_builtin_selection_preserves_browser_opt_out(monkeypatch, selection, expected):
    platform = types.ModuleType("core.platform_compat")
    platform.IS_WINDOWS = False
    platform.which_tool = lambda name: None
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform)
    spec = importlib.util.spec_from_file_location("relay_builtin_registration", ROOT / "src/builtin_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("FAUSTUS_BUILTIN_MCP", selection)
    monkeypatch.setattr(module, "MCP_DISABLED", False)
    monkeypatch.setattr(module, "builtin_python_env", lambda base: {"PYTHONPATH": base})
    calls = []
    tasks = []

    class Manager:
        async def connect_server(self, **kwargs):
            calls.append(kwargs)
            return True

    def spawn(coroutine):
        if coroutine.cr_code.co_name == "_start_npx_servers":
            coroutine.close()  # No unrelated NPX download or browser launch in this test.
        else:
            tasks.append(asyncio.create_task(coroutine))

    monkeypatch.setattr(module, "_spawn_bg", spawn)
    await module.register_builtin_servers(Manager())
    await asyncio.gather(*tasks)
    assert len(calls) == int(expected)
    if expected:
        assert calls[0]["server_id"] == "browser_session"
        assert calls[0]["transport"] == "stdio"
        assert Path(calls[0]["args"][0]) == ROOT / "src/browser_relay/server.py"
        assert Path(calls[0]["args"][0]).is_file()
    assert module._BUILTIN_NPX_SERVERS["builtin_browser"]["args"] == ["-y", "@playwright/mcp@latest"]
