"""Queued turns keep their place; the Sparks get their own concurrent lane."""
import asyncio

from src import agent_runs
from src.first_token_deadline import bounded


async def _slow_source(delay):
    await asyncio.sleep(delay)
    yield 'data: {"delta": "hola"}\n\n'


def test_queue_wait_does_not_spend_the_initial_budget():
    phase = {"now": "queue"}

    async def run():
        async def flip():
            await asyncio.sleep(0.25)
            phase["now"] = "model"
        asyncio.create_task(flip())
        return [c async for c in bounded(_slow_source(0.3), 0.1, phase=lambda: phase["now"])]

    out = asyncio.run(run())
    assert out == ['data: {"delta": "hola"}\n\n']


def test_model_phase_still_times_out():
    async def run():
        return [c async for c in bounded(_slow_source(0.5), 0.1, phase="model")]

    out = asyncio.run(run())
    assert len(out) == 1 and "first_token_timeout" in out[0]


def test_sparks_lane_runs_several_turns_at_once(monkeypatch):
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default)
    assert agent_runs._Lane("sparks").limit == 8
    assert agent_runs._Lane("local").limit == 1
    assert agent_runs._Lane("api").limit == 0


def test_sparks_endpoint_is_recognised(monkeypatch):
    from src import llm_core
    import src.settings as settings
    monkeypatch.setattr(settings, "load_settings", lambda: {"sparks_endpoints": {"glm53-tp3": "sparks-x"}})
    monkeypatch.setattr(llm_core, "_endpoint_id_for_url", lambda url: "sparks-x" if "8002" in url else "other")
    assert llm_core.is_sparks_url("http://192.168.0.236:8002/v1")
    assert not llm_core.is_sparks_url("http://127.0.0.1:11434")