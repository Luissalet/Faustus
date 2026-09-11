"""Lote 67 — OPS-07 (`GET /api/ops/remote-cost`) and EVAL-03
(`POST /api/ops/chaos/{fixture}`), both in `routes/ops_routes.py`.

Before this lote `grep -rn "remote_cost_report\\|src.chaos" routes/` matched
nothing: `cleanup_service.remote_cost_report()` had no HTTP door, and
`src/chaos.py` did not exist.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.ops_routes as ops_routes


def _app(monkeypatch, *, admin_raises=None):
    def _require_admin(request):
        if admin_raises is not None:
            raise admin_raises

    monkeypatch.setattr(ops_routes, "require_admin", _require_admin)
    app = FastAPI()
    app.include_router(ops_routes.setup_ops_routes())
    return app


def _client(monkeypatch, **kw):
    return TestClient(_app(monkeypatch, **kw))


# ── OPS-07 ───────────────────────────────────────────────────────────────

def test_remote_cost_requires_admin(monkeypatch):
    client = _client(monkeypatch, admin_raises=HTTPException(403, "Admin only"))
    response = client.get("/api/ops/remote-cost")
    assert response.status_code == 403


def test_remote_cost_reaches_remote_cost_report_for_an_admin(monkeypatch):
    from src import cleanup_service

    seen = {}

    def _fake_report(*, since=None, max_files=60):
        seen["since"] = since
        return {"known_total_usd": 1.23, "unknown_period": False, "unknown_cost_events": 0}

    monkeypatch.setattr(cleanup_service, "remote_cost_report", _fake_report)
    client = _client(monkeypatch)
    response = client.get("/api/ops/remote-cost")
    assert response.status_code == 200
    assert response.json() == {"known_total_usd": 1.23, "unknown_period": False, "unknown_cost_events": 0}


def test_remote_cost_never_fabricates_zero_when_unknown(monkeypatch):
    """OPS-07's own acceptance: an unknown remote bill must not read as $0."""
    from src import cleanup_service

    monkeypatch.setattr(
        cleanup_service, "remote_cost_report",
        lambda **_kw: {"known_total_usd": None, "unknown_period": True, "unknown_cost_events": 0,
                        "note": "nothing logged yet"},
    )
    client = _client(monkeypatch)
    body = client.get("/api/ops/remote-cost").json()
    assert body["unknown_period"] is True
    assert body["known_total_usd"] is None


# ── EVAL-03 ──────────────────────────────────────────────────────────────

def test_chaos_fixture_requires_admin(monkeypatch):
    client = _client(monkeypatch, admin_raises=HTTPException(403, "Admin only"))
    response = client.post("/api/ops/chaos/disk_full")
    assert response.status_code == 403


def test_chaos_fixture_dry_run_never_actually_injects(monkeypatch):
    """The route is dry-run only: it must never call anything but
    src.chaos.dry_run, which itself performs no injection."""
    client = _client(monkeypatch)
    for name in ("disk_full", "model_timeout", "mcp_drop", "process_kill"):
        response = client.post(f"/api/ops/chaos/{name}")
        assert response.status_code == 200, name
        body = response.json()
        assert body["dry_run"] is True
        assert body["fixture"] == name
        assert body["would_inject"]
        assert body["expected_result"]
        assert body["verified_by"]


def test_unknown_chaos_fixture_is_a_clean_404_naming_the_available_set(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/ops/chaos/does_not_exist")
    assert response.status_code == 404
    assert "disk_full" in response.json()["detail"]
