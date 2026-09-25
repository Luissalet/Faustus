"""The local model slot says who holds it while a request waits, reclaims a
slot whose holder task already finished, and an admin route lists the
server's asyncio tasks (live, exam run 18: a recovery step waited on
something for a quarter of an hour with the model server idle and no log)."""
import asyncio

from routes import debug_tasks_routes
from src import llm_core


def test_an_orphaned_slot_is_reclaimed(monkeypatch, caplog):
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAIT_LOG_SECONDS", 0.05)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})

    async def scenario():
        async def holder():
            await llm_core._LOCAL_MODEL_LOCK.acquire()
            llm_core._LOCAL_MODEL_CURRENT.update({"task": asyncio.current_task(), "owner": asyncio.current_task(), "workload": "foreground",
                                                   "model": "m", "started": 0})
            # finishes without ever releasing: its generator was abandoned
        t = asyncio.create_task(holder())
        await t
        assert llm_core._LOCAL_MODEL_LOCK.locked()
        await asyncio.wait_for(llm_core._acquire_local_model_lock("m2"), timeout=2)
        assert llm_core._LOCAL_MODEL_LOCK.locked()
        llm_core._LOCAL_MODEL_LOCK.release()
        llm_core._LOCAL_MODEL_CURRENT.clear()

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    text = caplog.text
    assert "waited" in text and "held by owner" in text
    assert "reclaiming the orphaned slot" in text


def test_a_live_holder_is_waited_for_and_reported(monkeypatch, caplog):
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAIT_LOG_SECONDS", 0.05)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})

    async def scenario():
        release = asyncio.Event()

        async def holder():
            await llm_core._LOCAL_MODEL_LOCK.acquire()
            llm_core._LOCAL_MODEL_CURRENT.update({"task": asyncio.current_task(), "owner": asyncio.current_task(), "workload": "foreground",
                                                   "model": "m", "started": 0})
            await release.wait()
            llm_core._LOCAL_MODEL_CURRENT.clear()
            llm_core._LOCAL_MODEL_LOCK.release()
        t = asyncio.create_task(holder())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(llm_core._acquire_local_model_lock("m2"))
        await asyncio.sleep(0.2)
        assert not waiter.done()  # a live holder is never robbed
        release.set()
        await asyncio.wait_for(waiter, timeout=2)
        llm_core._LOCAL_MODEL_LOCK.release()
        await t

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    assert "held by owner" in caplog.text
    assert "reclaiming" not in caplog.text


def test_task_snapshot_lists_tasks_and_the_slot():
    async def scenario():
        async def sleeper():
            await asyncio.sleep(5)
        t = asyncio.create_task(sleeper(), name="sleeper-under-test")
        await asyncio.sleep(0)
        snap = debug_tasks_routes.snapshot(frames=5)
        t.cancel()
        return snap

    snap = asyncio.run(scenario())
    names = [t["name"] for t in snap["tasks"]]
    assert "sleeper-under-test" in names
    entry = next(t for t in snap["tasks"] if t["name"] == "sleeper-under-test")
    assert any("sleeper" in line for line in entry["stack"])
    assert "locked" in snap["local_model_slot"]


def test_a_run_spread_over_step_tasks_is_one_owner(monkeypatch):
    """The agent loop body advances one step per new task
    (desktop_control_run); a nested call from the same run must not wait on
    the slot its own suspended stream holds."""
    import src.desktop_control_session as dcs
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: True, raising=False)
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: True, raising=False)
    monkeypatch.setattr(dcs, "_read", lambda name: {}, raising=False)
    monkeypatch.setattr(dcs, "_write", lambda name, data: None, raising=False)

    @dcs.desktop_control_run
    async def body():
        outer = llm_core._local_model_slot("http://127.0.0.1:8081/v1", "m")
        await outer.__aenter__()          # a stream takes the slot...
        yield "step 1"                    # ...and the run moves on in a new task
        async def nested():
            async with llm_core._local_model_slot("http://127.0.0.1:8081/v1", "m"):
                return "nested ran"
        yield await asyncio.wait_for(nested(), timeout=2)   # recovery from the same run
        await outer.__aexit__(None, None, None)

    async def scenario():
        return [chunk async for chunk in body()]

    assert asyncio.run(scenario()) == ["step 1", "nested ran"]


def test_another_run_still_waits_for_the_slot(monkeypatch):
    # A server with one generation pipe; several slots are shared (see
    # tests/test_local_slot_sharing.py).
    async def _one_slot(url):
        return 1
    monkeypatch.setattr(llm_core, "_server_slots", _one_slot)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: True, raising=False)
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: True, raising=False)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAIT_LOG_SECONDS", 0.05)
    from src.model_slot_owner import SLOT_OWNER

    async def scenario():
        SLOT_OWNER.set({"run": "A"})
        cm = llm_core._local_model_slot("http://127.0.0.1:8081/v1", "m")
        await cm.__aenter__()

        async def other_run():
            SLOT_OWNER.set({"run": "B"})
            async with llm_core._local_model_slot("http://127.0.0.1:8081/v1", "m"):
                return "B ran"
        t = asyncio.create_task(other_run())
        await asyncio.sleep(0.3)
        assert not t.done()   # a live run-level owner is never reclaimed
        await cm.__aexit__(None, None, None)
        return await asyncio.wait_for(t, timeout=2)

    assert asyncio.run(scenario()) == "B ran"
