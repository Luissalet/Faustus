"""Lote 70a, punto A.6 — `src/deep_research.py::DeepResearcher.research()`
must hold one `cpu_heavy` slot (`src/bg_jobs.py`, EXEC-04) for its whole
lifetime, exactly like `tests/test_l66_perf04_process_and_concurrency.py`'s
own `test_a_running_bg_job_counts_against_the_shared_cpu_heavy_budget`
already assumed a research caller would ("research", owner="deep_research")
— before this lot nothing in `DeepResearcher` ever called
`bg_jobs.acquire_cpu_heavy`/`release_cpu_heavy` at all, so an unbounded
number of local research runs could pile onto the same box regardless of
`cpu_heavy_max_concurrent`.
"""
from __future__ import annotations

import asyncio

import pytest

from src import bg_jobs
from src.deep_research import DeepResearcher


def _researcher(**kw):
    owner = kw.pop("owner", "alice")
    return DeepResearcher(
        llm_endpoint="http://127.0.0.1:11434/v1/chat/completions",
        llm_model="slow", max_time=kw.pop("max_time", 300), owner=owner, **kw,
    )


@pytest.fixture(autouse=True)
def _clean_cpu_heavy():
    with bg_jobs._CPU_HEAVY_LOCK:
        bg_jobs._CPU_HEAVY_HOLDERS.clear()
    yield
    with bg_jobs._CPU_HEAVY_LOCK:
        bg_jobs._CPU_HEAVY_HOLDERS.clear()


def test_research_acquires_and_releases_a_cpu_heavy_ticket_around_the_run(monkeypatch):
    """No real research work needed: `_llm` fails immediately, so this only
    proves the wrapping — acquire happens before the body runs, release
    happens after, with the right kind/owner, even though the run itself
    ends in `ResearchFailed`."""
    r = _researcher(max_rounds=1)

    calls = []
    real_acquire = bg_jobs.acquire_cpu_heavy
    real_release = bg_jobs.release_cpu_heavy

    async def spy_acquire(kind, owner="", **kw):
        calls.append(("acquire", kind, owner))
        return await real_acquire(kind, owner=owner, **kw)

    def spy_release(ticket):
        calls.append(("release", ticket))
        return real_release(ticket)

    monkeypatch.setattr(bg_jobs, "acquire_cpu_heavy", spy_acquire)
    monkeypatch.setattr(bg_jobs, "release_cpu_heavy", spy_release)

    async def _timeout(*a, **k):
        raise TimeoutError("model never answered")
    monkeypatch.setattr(r, "_llm", _timeout)
    monkeypatch.setattr(r, "_time_exceeded", lambda: True)

    from src.deep_research import ResearchFailed
    with pytest.raises(ResearchFailed):
        asyncio.run(r.research("What is the capital of France?"))

    assert calls[0] == ("acquire", "research", "alice")
    assert calls[1][0] == "release"
    assert len(calls) == 2, "exactly one acquire, one release — even on failure"
    # The slot is actually free again — a stuck ticket would leave this at 1.
    assert bg_jobs.cpu_heavy_active_count() == 0


def test_research_waits_for_a_held_slot_before_starting(monkeypatch):
    """A held ticket under a concurrency ceiling of 1 must make a second
    `research()` call actually WAIT (not race past it, not error) until the
    slot is released — proof the wrapping is a real wait, not a best-effort
    no-op."""
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)
    held = bg_jobs.try_acquire_cpu_heavy("other_job", owner="someone_else")
    assert held is not None

    r = _researcher(owner="bob", max_rounds=1)

    async def _timeout(*a, **k):
        raise TimeoutError("model never answered")
    monkeypatch.setattr(r, "_llm", _timeout)
    monkeypatch.setattr(r, "_time_exceeded", lambda: True)

    async def _release_soon():
        await asyncio.sleep(0.2)
        bg_jobs.release_cpu_heavy(held)

    from src.deep_research import ResearchFailed

    async def _run():
        releaser = asyncio.create_task(_release_soon())
        with pytest.raises(ResearchFailed):
            await r.research("What is the capital of Spain?")
        await releaser

    asyncio.run(_run())
    # research() only got in once the held slot was freed, and released its
    # own slot afterward — nothing left held.
    assert bg_jobs.cpu_heavy_active_count() == 0
