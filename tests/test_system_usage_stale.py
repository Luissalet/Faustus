"""The usage widget must never make anyone wait for it.

Found by using the app. The server log, while a turn was running:

    slow_request GET /api/system/usage status=200 elapsed=8.366s
    slow_request GET /api/system/usage status=200 elapsed=9.417s
    slow_request GET /api/system/usage status=200 elapsed=10.498s
    slow_request GET /api/system/usage status=200 elapsed=11.610s
    slow_request GET /api/system/usage status=200 elapsed=12.924s

Climbing, and 89 calls in three minutes. Collecting the snapshot probes every
llama.cpp engine (/health, /models, /props, /slots each) and an engine busy
generating answers those slowly -- so the collection took longer than the one
second its cache lived, every poll missed, and each one then queued on the
lock behind the one before it. The browser polls on a timer, so the queue only
ever grew.

These numbers are a gauge. A gauge two seconds out of date is worth far more
than one that blocks the interface for twelve.
"""
import asyncio

import pytest

import routes.system_usage_routes as usage


@pytest.fixture(autouse=True)
def _clean_cache():
    usage._cache["ts"] = 0.0
    usage._cache["data"] = None
    yield
    usage._cache["ts"] = 0.0
    usage._cache["data"] = None


def test_a_fresh_cache_is_served_without_collecting(monkeypatch):
    called = []

    async def never(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(usage, "_collect_usage_uncached", never)
    usage._cache["ts"] = usage.time.time()
    usage._cache["data"] = {"gpu": "fresh"}
    assert asyncio.run(usage.collect_usage()) == {"gpu": "fresh"}
    assert called == []


def test_a_stale_cache_is_served_immediately(monkeypatch):
    """The whole point: the caller gets last reading now, not in twelve
    seconds."""
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)
        return {"gpu": "fresh"}

    monkeypatch.setattr(usage, "_collect_usage_uncached", slow)

    async def scenario():
        usage._cache["ts"] = usage.time.time() - 2  # stale, but inside
        # `_CACHE_MAX_STALE`: past that ceiling the caller is meant to
        # WAIT for a fresh reading rather than be handed an old one
        # forever, so an hour would be testing the opposite rule.
        usage._cache["data"] = {"gpu": "stale"}
        out = await asyncio.wait_for(usage.collect_usage(), timeout=1.0)
        await asyncio.sleep(0)  # let the background task start
        return out

    assert asyncio.run(scenario()) == {"gpu": "stale"}


def test_a_stale_read_starts_exactly_one_refresh(monkeypatch):
    """A burst of polls -- several tabs, or a fast timer -- must not start a
    collection each. That is the pile-up this replaces."""
    runs = []

    async def slow():
        runs.append(1)
        await asyncio.sleep(0.2)
        return {"gpu": "fresh"}

    monkeypatch.setattr(usage, "_collect_usage_uncached", slow)

    async def scenario():
        usage._cache["ts"] = usage.time.time() - 2  # stale, but inside
        # `_CACHE_MAX_STALE`: past that ceiling the caller is meant to
        # WAIT for a fresh reading rather than be handed an old one
        # forever, so an hour would be testing the opposite rule.
        usage._cache["data"] = {"gpu": "stale"}
        for _ in range(10):
            await usage.collect_usage()
        await asyncio.sleep(0.4)

    asyncio.run(scenario())
    assert len(runs) == 1


def test_a_cold_cache_still_collects(monkeypatch):
    """The first caller has nothing to be served, so it waits -- once."""
    async def quick():
        usage._cache["ts"] = usage.time.time()
        usage._cache["data"] = {"gpu": "first"}
        return usage._cache["data"]

    monkeypatch.setattr(usage, "_collect_usage_uncached", quick)
    assert asyncio.run(usage.collect_usage()) == {"gpu": "first"}


def test_a_failing_refresh_leaves_the_last_good_reading(monkeypatch):
    """A detached task that raises must not take anything down, and must not
    blank the gauge either."""
    async def boom():
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr(usage, "_collect_usage_uncached", boom)

    async def scenario():
        usage._cache["ts"] = usage.time.time() - 2  # stale, but inside
        # `_CACHE_MAX_STALE`: past that ceiling the caller is meant to
        # WAIT for a fresh reading rather than be handed an old one
        # forever, so an hour would be testing the opposite rule.
        usage._cache["data"] = {"gpu": "stale"}
        out = await usage.collect_usage()
        await asyncio.sleep(0.05)
        return out, usage._cache["data"]

    served, after = asyncio.run(scenario())
    assert served == {"gpu": "stale"}
    assert after == {"gpu": "stale"}


def test_a_reading_older_than_the_ceiling_is_not_served(monkeypatch):
    """Serving stale numbers keeps the widget responsive. Serving them with
    no ceiling would leave a gauge on screen that quietly stopped being about
    now -- every refresh behind it failing, and nobody told. Past
    `_CACHE_MAX_STALE` the caller waits for a real reading instead."""
    async def fresh():
        usage._cache["ts"] = usage.time.time()
        usage._cache["data"] = {"gpu": "fresh"}
        return usage._cache["data"]

    monkeypatch.setattr(usage, "_collect_usage_uncached", fresh)

    async def scenario():
        usage._cache["ts"] = usage.time.time() - (usage._CACHE_MAX_STALE + 1)
        usage._cache["data"] = {"gpu": "ancient"}
        return await usage.collect_usage()

    assert asyncio.run(scenario()) == {"gpu": "fresh"}


def test_the_ceiling_is_well_past_the_slowest_collection():
    """12.9 s was the slowest collection measured with a turn running; a
    ceiling anywhere near that would put the fast path out of reach."""
    assert usage._CACHE_MAX_STALE >= 20.0
    assert usage._CACHE_MAX_STALE > usage._CACHE_TTL
