"""H01: distinguish interpreter isolation from host confinement."""
import json

import pytest

pytestmark = pytest.mark.usefixtures("code_mode_host_runtime")

from src.code_mode.runner import run_code_mode


@pytest.mark.asyncio
async def test_direct_python_host_write_is_disclosed(tmp_path):
    outside_guest = tmp_path / "host-sentinel.txt"
    result = await run_code_mode(
        "from pathlib import Path\n"
        f"Path({json.dumps(str(outside_guest))}).write_text('synthetic', encoding='utf-8')\n"
        "print('finished')\n",
        workspace=str(tmp_path / "workspace"),
    )
    assert result["exit_code"] == 0, result
    assert outside_guest.read_text(encoding="utf-8") == "synthetic"
    assert result["calls_made"] == 0
    assert result["runtime_guarantees"] == {
        "mode": "host_process", "filesystem_isolated": False,
        "network_isolated": False, "tool_policy_scope": "tools.call_only",
    }


@pytest.mark.asyncio
async def test_runtime_error_still_reports_host_guarantees():
    result = await run_code_mode("raise RuntimeError('synthetic failure')")
    assert result["exit_code"] != 0
    assert result["runtime_guarantees"]["mode"] == "host_process"
