"""The shared LLM HTTP client must never hand a chat turn connections that
belong to a closed event loop (a background job's asyncio.run)."""
import asyncio
import threading

from src import llm_core


async def _get():
    return llm_core._get_http_client()


def _reset(monkeypatch):
    monkeypatch.setattr(llm_core, "_http_client", None)
    monkeypatch.setattr(llm_core, "_http_client_owner", None)


def test_client_made_in_a_finished_loop_is_replaced(monkeypatch):
    _reset(monkeypatch)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("c", asyncio.run(_get())))
    t.start(); t.join()
    first = box["c"]
    second = asyncio.run(_get())          # a new loop: the first one is closed
    assert second is not first
    assert llm_core._http_client is second


def test_same_loop_reuses_one_client(monkeypatch):
    _reset(monkeypatch)

    async def twice():
        return llm_core._get_http_client(), llm_core._get_http_client()

    a, b = asyncio.run(twice())
    assert a is b


def test_a_second_live_loop_gets_its_own_client(monkeypatch):
    _reset(monkeypatch)
    started, release, box = threading.Event(), threading.Event(), {}

    async def owner():
        box["owner"] = llm_core._get_http_client()
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    t = threading.Thread(target=lambda: asyncio.run(owner()))
    t.start()
    started.wait(5)
    try:
        other = asyncio.run(_get())
        assert other is not box["owner"]
        assert llm_core._http_client is box["owner"]   # the owner keeps it
    finally:
        release.set(); t.join(5)


def test_externally_set_client_is_adopted(monkeypatch):
    _reset(monkeypatch)
    sentinel = type("C", (), {"is_closed": False})()
    monkeypatch.setattr(llm_core, "_http_client", sentinel)
    assert asyncio.run(_get()) is sentinel
