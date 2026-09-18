"""tests/test_creator_resources_routes.py — WP30: GET /api/creator/resources.

Same `AUTH_ENABLED=false` no-login pattern every other Creator route test in
this repo uses (see tests/test_creator_wp02.py).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import gpu_shared_memory as gsm
from src import gpu_topology
from src import resource_admission
from src.contracts.inference import HardwareSnapshot
from src.creator import resources as creator_resources


@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("FAUSTUS_DATA_DIR", str(tmp_path))
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    monkeypatch.setattr(gpu_topology, "snapshot", lambda: HardwareSnapshot(host="localhost"))
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {"supported": False, "reason": "no gpu in test"})

    resource_admission.reset_all()
    creator_resources.reset_for_tests()

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_resources_routes import setup_creator_resources_routes
    app = FastAPI()
    app.include_router(setup_creator_resources_routes())
    client = TestClient(app)
    try:
        yield client
    finally:
        resource_admission.reset_all()
        creator_resources.reset_for_tests()


def test_flag_off_is_404_and_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_resources_routes import setup_creator_resources_routes
    app = FastAPI()
    app.include_router(setup_creator_resources_routes())
    client = TestClient(app)
    resp = client.get("/api/creator/resources")
    assert resp.status_code == 404


def test_resources_route_reports_inventory_mode_and_empty_queue(route_client):
    resp = route_client.get("/api/creator/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["creator_enabled"] is True
    assert body["inventory"]["mode"] == "serial"
    assert body["inventory"]["gpus"]["reading_ok"] is False
    assert body["active_admissions"] == []
    assert body["queue"] == []


def test_resources_route_lists_an_active_admission(route_client):
    footprint = creator_resources.Footprint(vram_bytes=1_000_000_000, source="estimated")
    admission = creator_resources.admit(footprint, "http://gpu1:8188", run_id="mrun_1")
    assert admission.ok is True

    resp = route_client.get("/api/creator/resources")
    body = resp.json()
    assert len(body["active_admissions"]) == 1
    assert body["active_admissions"][0]["run_id"] == "mrun_1"
    assert body["active_admissions"][0]["device"] == "http://gpu1:8188"

    creator_resources.release("mrun_1")
    resp2 = route_client.get("/api/creator/resources")
    assert resp2.json()["active_admissions"] == []
