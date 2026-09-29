import pytest

from src.code_mode import bridge, runner


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 3,
        "max_calls": 10, "max_output_bytes": 20000, "max_memory_bytes": 0})


@pytest.mark.asyncio
async def test_large_raw_stderr_without_newlines_cannot_block_guest():
    result = await runner.run_code_mode("import os; os.write(2,b'x'*2000000); print('finished')")
    assert result["exit_code"] == 0
    assert result["output"].strip() == "finished"


@pytest.mark.asyncio
async def test_stderr_drain_does_not_consume_tool_protocol(monkeypatch):
    calls = []
    async def dispatch(name, args, **kw):
        calls.append(name)
        return {"output": "bridge result", "exit_code": 0}
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode("import os\nos.write(2,b'x'*1000000)\n"
        "r=tools.call('read_file', {})\nos.write(2,b'y'*1000000)\nprint(r['output'])")
    assert result["exit_code"] == 0
    assert result["output"].strip() == "bridge result"
    assert calls == ["read_file"]
    assert result["tool_outcomes"]["counts"]["succeeded"] == 1


@pytest.mark.asyncio
async def test_abrupt_exit_exposes_only_bounded_latest_stderr_tail():
    result = await runner.run_code_mode("import os; os.write(2,b'x'*2000000+b'TAIL_MARKER'); os._exit(1)")
    assert result["exit_code"] == 1
    assert result["receipt"]["terminated_by"] == "error"
    assert len(result["stderr"]) == 2000
    assert result["stderr"].endswith("TAIL_MARKER")
