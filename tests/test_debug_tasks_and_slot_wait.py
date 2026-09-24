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
            llm_core._LOCAL_MODEL_CURRENT.update({"task": asyncio.current_task(), "workload": "foreground",
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
    assert "waited" in text and "held by task" in text
    assert "reclaiming the orphaned slot" in text


def test_a_live_holder_is_waited_for_and_reported(monkeypatch, caplog):
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAIT_LOG_SECONDS", 0.05)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})

    async def scenario():
        release = asyncio.Event()

        async def holder():
            await llm_core._LOCAL_MODEL_LOCK.acquire()
            llm_core._LOCAL_MODEL_CURRENT.update({"task": asyncio.current_task(), "workload": "foreground",
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
    assert "held by task" in caplog.text
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
