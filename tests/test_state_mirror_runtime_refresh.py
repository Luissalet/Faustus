import asyncio
from unittest.mock import AsyncMock

import pytest

from src.state_mirror import service as mirror


@pytest.fixture
def probe(monkeypatch):
    from src import agent_runs
    from routes import system_usage_routes
    monkeypatch.setattr(mirror, "enabled", lambda: True)
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [])
    collector = AsyncMock(return_value={"ts": 100})
    monkeypatch.setattr(system_usage_routes, "collect_usage", collector)
    return collector


def test_idle_scope_refreshes_once_before_using_cache(probe):
    assert asyncio.run(mirror.refresh_runtime_usage([{"owner": "alice"}, {"owner": "bob"}]))
    probe.assert_awaited_once_with()


@pytest.mark.parametrize("scopes", [[], [{}], [{"owner": ""}], [None]])
def test_unowned_scopes_never_probe(probe, scopes):
    assert not asyncio.run(mirror.refresh_runtime_usage(scopes))
    probe.assert_not_called()


def test_disabled_or_interactive_sweeps_do_not_poll_services(probe, monkeypatch):
    from src import agent_runs
    monkeypatch.setattr(mirror, "enabled", lambda: False)
    assert not asyncio.run(mirror.refresh_runtime_usage([{"owner": "alice"}]))
    monkeypatch.setattr(mirror, "enabled", lambda: True)
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: ["chat"])
    assert not asyncio.run(mirror.refresh_runtime_usage([{"owner": "alice"}]))
    probe.assert_not_called()


def test_failed_probe_does_not_refresh_old_timestamp(probe, monkeypatch):
    from routes import system_usage_routes
    cache = {"ts": 123, "data": {"ts": 123}}
    monkeypatch.setattr(system_usage_routes, "_cache", cache)
    probe.side_effect = RuntimeError("unavailable")
    assert not asyncio.run(mirror.refresh_runtime_usage([{"owner": "alice"}]))
    assert cache == {"ts": 123, "data": {"ts": 123}}


def test_shutdown_cancels_refresh(probe):
    probe.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mirror.refresh_runtime_usage([{"owner": "alice"}]))


def test_scheduler_collects_before_adapter_sweep(probe, monkeypatch):
    order = []
    scopes = [{"owner": "alice"}]
    async def collect():
        order.append("measured")
    probe.side_effect = collect
    monkeypatch.setattr(mirror, "run_scheduled", lambda received: order.append(("swept", received)))
    sleeps = []
    async def sleep(_):
        sleeps.append(True)
        if len(sleeps) == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mirror.scheduler_loop(scopes_provider=lambda: scopes, interval_s=5))
    assert order == ["measured", ("swept", scopes)]
