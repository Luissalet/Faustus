import pytest

from mcp_servers.hoard_copilot_server import _pulse_view


@pytest.mark.parametrize(("activity", "workers", "state", "waiting"), [
    ({}, 0, "idle", False),
    ({"running": ["open", "other"], "details": {"open": {"phase": "thinking"}}}, 0, "inferring", False),
    ({"running": ["open"], "details": {"open": {"phase": "tool"}}}, 0, "idle", False),
    ({"running": ["open"], "awaiting_approval": ["open"]}, 1, "background", True),
    ({"running": ["open"], "queued": {"open": 1}}, 0, "idle", True),
    ({"running": ["other"], "queued": {"other": 1}}, 0, "idle", False),
    ({"workers": {"child": {"parent": "open"}}}, 0, "background", False),
    ({"active_automations": 1}, None, "background", False),
    ({"unverified": 1}, 0, None, False),
])
def test_mcp_pulse_projection(activity, workers, state, waiting):
    result = _pulse_view(activity, "open", workers)
    assert result["state"] == state
    assert result["waiting"] is waiting
    assert result["active_agora_workers"] == workers
