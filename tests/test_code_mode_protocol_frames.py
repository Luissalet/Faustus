import pytest

pytestmark = pytest.mark.usefixtures("code_mode_host_runtime")

from src.code_mode import bridge, runner


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 3,
        "max_calls": 10, "max_output_bytes": 200000, "max_memory_bytes": 0})


@pytest.mark.asyncio
async def test_result_above_default_stream_limit_below_output_quota_is_complete():
    result = await runner.run_code_mode("print('x'*100000)")
    assert result["exit_code"] == 0
    assert result["output"] == "x" * 100000 + "\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["\x00", "\u00e9", "\U0001f642"])
async def test_json_escape_expansion_keeps_complete_output(value):
    result = await runner.run_code_mode(f"print({value!r}*24000)")
    assert result["exit_code"] == 0
    assert result["output"] == value * 24000 + "\n"


@pytest.mark.asyncio
async def test_oversized_call_frame_is_rejected_before_dispatch(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 3,
        "max_calls": 10, "max_output_bytes": 100, "max_memory_bytes": 0})
    calls = []
    async def dispatch(*a, **kw):
        calls.append(a)
        return {"exit_code": 0}
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode("tools.call('write_file', {'body':'x'*2000000})")
    assert result["exit_code"] == 1
    assert result["receipt"]["terminated_by"] == "protocol_frame_limit"
    assert result["tool_outcomes"]["calls"] == 0
    assert calls == []


@pytest.mark.asyncio
async def test_framing_allowance_does_not_raise_guest_output_quota(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 3,
        "max_calls": 10, "max_output_bytes": 100, "max_memory_bytes": 0})
    result = await runner.run_code_mode("print('x'*101)")
    assert result["exit_code"] == 1
    assert result["receipt"]["terminated_by"] == "output"
