import asyncio
import pytest
from src import agent_runs, llm_core


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: False if key == "agent_runs_persist" else default)
    monkeypatch.setattr(llm_core, "_sparks_initial_wait", lambda url: 0.04 if url == "spark-test" else None)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


@pytest.mark.asyncio
async def test_global_queue_wait_times_out_without_entering_model():
    lane = agent_runs._lane("local")
    lane.active = lane.limit
    entered = []
    async def body():
        entered.append(True)
        yield 'data: {"delta":"answer"}\n\n'
    run = agent_runs.start("queued-spark", body(), lane="local", endpoint_url="spark-test")
    await asyncio.wait_for(run.task, 0.5)
    assert run.status == "error"
    assert not entered
    assert not lane.waiting
    assert any('first_token_timeout' in event for event in run.buffer)
    if run.evict_task:
        run.evict_task.cancel()


@pytest.mark.asyncio
async def test_preparation_timeout_closes_generator_and_marks_error():
    closed = []
    async def body():
        try:
            yield 'data: {"type":"status","status":"preparing"}\n\n'
            await asyncio.sleep(10)
        finally:
            closed.append(True)
    run = agent_runs.start("preparing-spark", body(), endpoint_url="spark-test")
    await asyncio.wait_for(run.task, 0.5)
    assert closed == [True]
    assert run.status == "error"
    assert any('first_token_timeout' in event for event in run.buffer)
    if run.evict_task:
        run.evict_task.cancel()
