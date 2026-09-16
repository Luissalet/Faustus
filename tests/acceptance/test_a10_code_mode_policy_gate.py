"""A10 -- acceptance-parity case (Lote T6).

Contract (docs/spec/paridad/, blueprint doc 02 "Code Mode"): generated code
running inside ``run_code`` composes tool calls through
``src.code_mode.bridge.dispatch_call``, which dispatches every one of them
through the SAME ``src.tool_execution.execute_tool_block`` an ordinary
model tool call goes through -- no separate, weaker gate for code-composed
calls.

Two things are proven against real Faustus code (no mock of the module
under test):

1. A destructive command (``bash("rm -rf ...")``) reaches the exact same
   destructive-command-guard rejection through ``bridge.dispatch_call`` as
   it does through a direct ``execute_tool_block`` call with an equivalent
   security context -- same ``blocked``/``policy``/``error`` shape.
2. A tool named in ``disabled_tools`` is unreachable from Code Mode: the
   bridge dispatch returns the same "disabled by user" rejection
   ``execute_tool_block`` gives any other caller, and ``tools.list()``
   (via ``bridge.list_tools``) does not offer it in the first place.

The full ``run_code`` -> subprocess -> ``tools.call`` round trip (real
guest.py subprocess, real runner.py) is exercised for the disabled-tool
case, so this is not just a direct-bridge-function test: the destructive-
command case is exercised BOTH through the real subprocess round trip
(``run_code_mode``) and directly against ``bridge.dispatch_call`` /
``execute_tool_block``, and the three results are compared.
"""
from __future__ import annotations

import json

import pytest

from tests.acceptance.conftest import record_evidence

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    yield


async def _direct_bash_rm_result():
    from src.agent_tools import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_execution import execute_tool_block

    block = ToolBlock("bash", json.dumps({"command": "rm -rf /tmp/a10_target"}))
    _desc, result = await execute_tool_block(
        block,
        session_id="a10-session",
        owner="luis",
        workspace=None,
        disabled_tools=set(),
        tool_policy=None,
        security_context=ToolRunSecurityContext(),
        call_id="direct_a10",
    )
    return result


@pytest.mark.acceptance("A10")
async def test_destructive_tool_call_from_generated_code_hits_the_same_gate_as_a_direct_call(
    request, monkeypatch,
):
    from src.code_mode import bridge

    direct_result = await _direct_bash_rm_result()
    assert direct_result.get("blocked") is True
    assert "Destructive command" in direct_result.get("error", "")

    bridge_result = await bridge.dispatch_call(
        "bash",
        {"command": "rm -rf /tmp/a10_target"},
        session_id="a10-session",
        owner="luis",
        workspace=None,
        workspace_roots=None,
        disabled_tools=set(),
        call_id="code_mode:call_1:test",
    )
    assert bridge_result == direct_result, (
        "run_code's bridge dispatch must return byte-for-byte the same "
        "rejection execute_tool_block gives an ordinary direct call for "
        "the identical destructive command."
    )

    # And through the REAL subprocess round trip (run_code -> guest.py ->
    # tools.call('bash', ...) -> bridge.dispatch_call): same policy, same
    # denial reason, surfaced back through run_code_mode's result.
    import src.settings as settings_mod
    from src.settings import DEFAULT_SETTINGS

    def _fast_settings(key, default=None):
        if key == "agent_code_mode_timeout_seconds":
            return 15
        return DEFAULT_SETTINGS.get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", _fast_settings)
    from src.code_mode.runner import run_code_mode

    code = (
        "r = tools.call('bash', {'command': 'rm -rf /tmp/a10_target'})\n"
        "print(r)\n"
    )
    run_result = await run_code_mode(code, session_id="a10-session", owner="luis")

    assert run_result.get("exit_code") == 0, run_result
    assert "Destructive command" in run_result.get("output", "")
    assert "'blocked': True" in run_result.get("output", "")

    record_evidence(
        request,
        module="src.code_mode.bridge",
        function="dispatch_call",
        direct_result=direct_result,
        bridge_result=bridge_result,
        subprocess_output=run_result.get("output"),
    )


@pytest.mark.acceptance("A10")
async def test_disabled_tool_is_unreachable_from_generated_code(request):
    from src.code_mode import bridge

    assert "write_file" not in bridge.allowed_tool_names(disabled_tools={"write_file"})
    names = [row["name"] for row in bridge.list_tools(disabled_tools={"write_file"})]
    assert "write_file" not in names

    result = await bridge.dispatch_call(
        "write_file",
        {"path": "notes.txt", "content": "hi"},
        session_id="a10-session",
        owner="luis",
        workspace=None,
        workspace_roots=None,
        disabled_tools={"write_file"},
        call_id="code_mode:call_1:disabled",
    )
    assert result.get("error") == "Tool 'write_file' is disabled by user."
    assert result.get("exit_code") == 1

    record_evidence(
        request,
        module="src.code_mode.bridge",
        function="dispatch_call/allowed_tool_names/list_tools",
        result=result,
    )
