"""Lot L wiring gap: `app.py` (integrator-owned, not touched by this lot)
must call `model_lease.start()`/`model_lease.stop()` next to
`model_warmup.start()`/`stop()` in its startup/shutdown lifespan, and
`GET /api/health` must report `service`/`instance_id`/`leases` so a
machine running several instances can tell which one answered. The exact
diff is in `LOT_L_wiring.md` at the worktree root. Once applied, these flip
from `xfail(strict=True)` to a plain passing assertion — same shape as
`tests/test_t7_wiring.py`/`tests/test_l55_app_wiring.py` for other lots'
wiring gaps.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import app as app_module


def test_health_check_reports_service_and_instance_id():
    out = asyncio.run(app_module.health_check())
    assert out.get("service") == "faustus"
    assert "instance_id" in out
    assert "leases" in out


def test_lifespan_starts_and_stops_the_model_lease():
    source = inspect.getsource(app_module)
    assert "model_lease.start()" in source
    assert "model_lease.stop()" in source
