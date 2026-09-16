"""Core Channel/Backend fallback semantics (R1) — no network."""
from __future__ import annotations

import asyncio

import pytest

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult, clear_availability_cache


class _FailBackend(Backend):
    name = "fail"

    async def read(self, url_or_id: str, **kwargs):
        raise ReachBackendError("boom")


class _UnsupportedBackend(Backend):
    name = "unsupported"
    # read/search not overridden -> NotImplementedError from the base class


class _OkBackend(Backend):
    name = "ok"

    async def read(self, url_or_id: str, **kwargs):
        return ReachResult(channel="test", text=f"read {url_or_id}", source_trust="scrape")

    async def search(self, query: str, **kwargs):
        return [ReachResult(channel="test", text=f"hit for {query}")]


class _TestChannel(Channel):
    name = "test"
    backends = [_FailBackend(), _UnsupportedBackend(), _OkBackend()]


def test_read_falls_back_through_every_failing_backend_and_records_attempts():
    channel = _TestChannel()
    result = asyncio.run(channel.read("http://example.com"))
    assert result.backend == "ok"
    assert result.text == "read http://example.com"
    assert len(result.attempts) == 3
    assert result.attempts[0] == {"backend": "fail", "ok": False, "reason": "boom"}
    assert result.attempts[1]["backend"] == "unsupported"
    assert result.attempts[1]["ok"] is False
    assert result.attempts[2] == {"backend": "ok", "ok": True, "reason": ""}
    assert result.fetched_at  # stamped by the channel


def test_read_reports_no_backend_available_without_raising():
    class _AllFailChannel(Channel):
        name = "allfail"
        backends = [_FailBackend()]

    result = asyncio.run(_AllFailChannel().read("x"))
    assert result.backend == ""
    assert result.error == "boom"
    assert result.attempts == [{"backend": "fail", "ok": False, "reason": "boom"}]


def test_search_falls_back_and_tags_channel_backend_on_every_hit():
    channel = _TestChannel()
    results = asyncio.run(channel.search("query"))
    assert len(results) == 1
    assert results[0].channel == "test"
    assert results[0].backend == "ok"
    assert results[0].text == "hit for query"


def test_availability_is_cached_per_backend_and_live_flag():
    clear_availability_cache()
    calls = {"n": 0}

    class _CountingBackend(Backend):
        name = "counting"

        async def _probe(self, live: bool) -> Availability:
            calls["n"] += 1
            return Availability(status="ready", checked_live=live)

    backend = _CountingBackend()
    asyncio.run(backend.available(live=False))
    asyncio.run(backend.available(live=False))
    assert calls["n"] == 1  # second call served from the 10-minute cache

    asyncio.run(backend.available(live=True))
    assert calls["n"] == 2  # live=True is a distinct cache key


def test_available_never_raises_even_when_probe_throws():
    class _BrokenBackend(Backend):
        name = "broken"

        async def _probe(self, live: bool) -> Availability:
            raise RuntimeError("network is down")

    clear_availability_cache()
    result = asyncio.run(_BrokenBackend().available(live=True))
    assert result.status == "unavailable"
    assert "network is down" in result.reason
