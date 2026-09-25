"""Two foreground calls to the same llama-server and model share its slots
instead of waiting for each other; anything that could load another model,
or run in the background, still waits for the one slot."""
import asyncio

import pytest

from src import llm_core
from src.model_slot_owner import SLOT_OWNER

URL = "http://127.0.0.1:8081/v1"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCK", asyncio.Lock())
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_SHARED", {"host": "", "model": "", "count": 0})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: True, raising=False)
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: True, raising=False)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAIT_LOG_SECONDS", 0.05)
    slots = {"n": 4}

    async def _slots(url):
        return slots["n"]
    monkeypatch.setattr(llm_core, "_server_slots", _slots)
    return slots


async def _hold(run, url=URL, model="m", workload=None):
    SLOT_OWNER.set({"run": run})
    cm = llm_core._local_model_slot(url, model, workload)
    await cm.__aenter__()
    return cm


async def _try(run, url=URL, model="m", workload=None):
    SLOT_OWNER.set({"run": run})
    async with llm_core._local_model_slot(url, model, workload):
        return run


def test_a_second_chat_on_the_same_server_and_model_runs_beside_the_first(gate):
    async def scenario():
        cm = await _hold("A")
        got = await asyncio.wait_for(_try("B"), timeout=1)
        await cm.__aexit__(None, None, None)
        return got
    assert asyncio.run(scenario()) == "B"


def test_never_more_calls_than_the_server_has_slots(gate):
    gate["n"] = 2

    async def scenario():
        cm = await _hold("A")
        b = asyncio.create_task(_hold("B"))
        cm_b = await asyncio.wait_for(b, timeout=1)       # joins: 2 of 2 slots
        c = asyncio.create_task(_try("C"))
        await asyncio.sleep(0.2)
        assert not c.done()                               # a third one waits
        await cm_b.__aexit__(None, None, None)
        await cm.__aexit__(None, None, None)
        return await asyncio.wait_for(c, timeout=2)
    assert asyncio.run(scenario()) == "C"


@pytest.mark.parametrize("other", [
    {"model": "another-model"},
    {"url": "http://127.0.0.1:8082/v1"},
    {"workload": "background"},
])
def test_another_model_server_or_background_work_still_waits(gate, other):
    async def scenario():
        cm = await _hold("A")
        t = asyncio.create_task(_try("B", **other))
        await asyncio.sleep(0.2)
        assert not t.done()
        await cm.__aexit__(None, None, None)
        return await asyncio.wait_for(t, timeout=2)
    assert asyncio.run(scenario()) == "B"


def test_one_slot_servers_are_not_shared(gate):
    gate["n"] = 1

    async def scenario():
        cm = await _hold("A")
        t = asyncio.create_task(_try("B"))
        await asyncio.sleep(0.2)
        assert not t.done()
        await cm.__aexit__(None, None, None)
        return await asyncio.wait_for(t, timeout=2)
    assert asyncio.run(scenario()) == "B"


def test_a_new_holder_for_another_model_waits_for_the_calls_sharing_the_old_one(gate):
    async def scenario():
        cm = await _hold("A")
        cm_b = await asyncio.wait_for(_hold("B"), timeout=1)       # shares A's server
        await cm.__aexit__(None, None, None)                        # A done, B still running
        t = asyncio.create_task(_try("C", model="another-model"))   # would load a model
        await asyncio.sleep(0.2)
        assert not t.done()
        await cm_b.__aexit__(None, None, None)
        return await asyncio.wait_for(t, timeout=2)
    assert asyncio.run(scenario()) == "C"
