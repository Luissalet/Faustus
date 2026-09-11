"""Tests for ADP-32: `src/resource_admission.py` and `GET /api/ops/admission`.

`src.llm_core._LOCAL_MODEL_LOCK` is left untouched (the ficha is explicit
about that) — these tests are entirely against the new, unwired module: pool
definition never lets two URLs of one server create double capacity, the
serial default and a configured `max_concurrent` are both honoured under
real concurrent load, a `"foreground"` acquire jumps a queued `"background"`
one, cancellation frees a held slot, and a duplicate/late `release()` is a
no-op rather than a chance to reopen a slot a cancellation already returned.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.ops_routes as ops_routes
from src import resource_admission as ra


@pytest.fixture(autouse=True)
def _clean_pools():
    ra.reset_all()
    try:
        yield
    finally:
        ra.reset_all()


# ── pool definition: no fictitious capacity ─────────────────────────────────

def test_two_urls_of_the_same_server_share_one_pool():
    pool = ra.define_pool(
        "ollama-a", "gpu",
        ["http://127.0.0.1:11434/v1/chat/completions", "http://127.0.0.1:11434/api/generate"],
        max_concurrent=2,
    )
    assert pool["endpoints"] == ["http://127.0.0.1:11434"]
    assert ra.pool_for_endpoint("http://127.0.0.1:11434/api/tags") == "ollama-a"


def test_a_second_pool_cannot_claim_an_already_pooled_endpoint():
    ra.define_pool("pool-a", "gpu", ["http://host:9999/v1/chat/completions"], max_concurrent=2)
    with pytest.raises(ra.PoolError):
        ra.define_pool("pool-b", "gpu", ["http://host:9999/api/generate"], max_concurrent=2)
    # And the first pool's claim is unharmed by the failed attempt.
    assert ra.pool_for_endpoint("http://host:9999/anything") == "pool-a"


def test_max_concurrent_defaults_to_one_serial_slot():
    pool = ra.define_pool("no-config", "cpu", ["http://host:1"])
    assert pool["max_concurrent"] == 1


def test_unknown_kind_is_rejected():
    with pytest.raises(ra.PoolError):
        ra.define_pool("bad-kind", "quantum", [])


def test_redefining_a_pool_releases_endpoints_it_no_longer_claims():
    ra.define_pool("shrinking", "remote", ["http://a:1", "http://b:1"], max_concurrent=1)
    ra.define_pool("shrinking", "remote", ["http://a:1"], max_concurrent=1)
    assert ra.pool_for_endpoint("http://b:1") is None
    # Now a different pool may legitimately claim the endpoint that was freed.
    ra.define_pool("other", "remote", ["http://b:1"], max_concurrent=1)
    assert ra.pool_for_endpoint("http://b:1") == "other"


# ── admission: the configured limit is a real ceiling ───────────────────────

async def test_admission_never_exceeds_configured_max_concurrent():
    ra.define_pool("busy", "cpu", ["http://host:1"], max_concurrent=2)
    concurrent = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal concurrent, peak
        async with ra.lease("busy", priority="background"):
            async with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    assert peak == 2
    assert ra.status()["pools"][0]["in_use"] == 0  # every lease released cleanly


async def test_serial_default_admits_one_call_at_a_time():
    ra.define_pool("serial", "gpu", ["http://host:2"])  # no max_concurrent given
    order = []

    async def worker(name, hold):
        async with ra.lease("serial", priority="background"):
            order.append(("start", name))
            await asyncio.sleep(hold)
            order.append(("end", name))

    await asyncio.gather(worker("a", 0.03), worker("b", 0.0))
    # "a" must fully finish before "b" starts: only one slot ever existed.
    assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


# ── priority: foreground jumps a queued background waiter ──────────────────

async def test_foreground_priority_jumps_a_queued_background_waiter():
    ra.define_pool("prio", "gpu", ["http://host:3"], max_concurrent=1)
    order = []
    holder = await ra.acquire("prio", priority="background", owner="holder")

    async def background_waiter():
        async with ra.lease("prio", priority="background", owner="bg"):
            order.append("background")

    async def foreground_waiter():
        async with ra.lease("prio", priority="foreground", owner="fg"):
            order.append("foreground")

    bg_task = asyncio.create_task(background_waiter())
    await asyncio.sleep(0.01)  # background is queued first
    fg_task = asyncio.create_task(foreground_waiter())
    await asyncio.sleep(0.01)  # foreground joins the queue behind it
    await ra.release(holder.id)  # a slot opens with both still waiting
    await asyncio.gather(bg_task, fg_task)
    assert order == ["foreground", "background"]


# ── cancellation releases; a late/duplicate release never reopens ──────────

async def test_cancelling_the_holder_releases_its_slot():
    ra.define_pool("cancel-pool", "cpu", ["http://host:4"], max_concurrent=1)

    async def hold_forever():
        async with ra.lease("cancel-pool", priority="background"):
            await asyncio.sleep(10)

    task = asyncio.create_task(hold_forever())
    await asyncio.sleep(0.01)
    assert ra.status()["pools"][0]["in_use"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ra.status()["pools"][0]["in_use"] == 0  # the finally-block released it


async def test_a_late_duplicate_release_does_not_reopen_capacity():
    ra.define_pool("late", "cpu", ["http://host:5"], max_concurrent=1)
    leased = await ra.acquire("late", priority="background")
    assert await ra.release(leased.id) is True
    assert ra.status()["pools"][0]["in_use"] == 0
    # The "late result" for a job that was already reconciled: releasing the
    # same id again must be a no-op, never a negative in_use or a phantom
    # extra slot for the next acquire to exploit.
    assert await ra.release(leased.id) is False
    assert ra.status()["pools"][0]["in_use"] == 0

    second = await ra.acquire("late", priority="background", timeout=0.2)
    assert second.id != leased.id
    assert ra.status()["pools"][0]["in_use"] == 1
    await ra.release(second.id)


async def test_acquire_times_out_rather_than_hanging_when_the_pool_stays_full():
    ra.define_pool("full", "cpu", ["http://host:6"], max_concurrent=1)
    holder = await ra.acquire("full", priority="background")
    with pytest.raises(ra.AdmissionTimeout):
        await ra.acquire("full", priority="background", timeout=0.05)
    await ra.release(holder.id)


# ── GET /api/ops/admission ──────────────────────────────────────────────────

def _app(monkeypatch, *, admin_raises=None):
    def _require_admin(request):
        if admin_raises is not None:
            raise admin_raises

    monkeypatch.setattr(ops_routes, "require_admin", _require_admin)
    app = FastAPI()
    app.include_router(ops_routes.setup_ops_routes())
    return app


def test_admission_status_requires_admin(monkeypatch):
    client = TestClient(_app(monkeypatch, admin_raises=HTTPException(403, "Admin only")))
    response = client.get("/api/ops/admission")
    assert response.status_code == 403


def test_admission_status_reports_defined_pools_over_http(monkeypatch):
    ra.define_pool("http-pool", "remote", ["http://host:7"], max_concurrent=3)
    client = TestClient(_app(monkeypatch))
    response = client.get("/api/ops/admission")
    assert response.status_code == 200
    pools = {p["pool_id"]: p for p in response.json()["pools"]}
    assert pools["http-pool"]["max_concurrent"] == 3
    assert pools["http-pool"]["in_use"] == 0
    assert pools["http-pool"]["available"] == 3
