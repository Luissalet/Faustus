from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.branching_futures_routes as branching_routes
import routes.immune_system_routes as immune_routes
import routes.teach_mode_routes as teach_routes
from src.branching_futures.service import BranchingService
from src.durable_feature_store import DurableFeatureStore
from src.immune_system.service import ImmuneService
from src.teach_mode.service import TeachService


def _client(router_module, setup, service_name, service, monkeypatch):
    monkeypatch.setattr(router_module, "require_admin", lambda _request: None)
    monkeypatch.setattr(router_module, "_owner", lambda _request: "alice")
    monkeypatch.setattr(router_module, "enabled", lambda: True)
    monkeypatch.setattr(router_module, service_name, lambda: service)
    app = FastAPI()
    app.include_router(setup())
    return TestClient(app)


def test_teach_routes_start_and_read_owner_scoped_demonstration(tmp_path, monkeypatch):
    service = TeachService(DurableFeatureStore(str(tmp_path / "teach.db")))
    client = _client(
        teach_routes, teach_routes.setup_teach_mode_routes, "teach_service_module",
        service, monkeypatch,
    )
    created = client.post(
        "/api/teach/demonstrations",
        json={"title": "Release", "intent": "Release safely", "session_id": "s1"},
    )
    assert created.status_code == 200 and created.json()["ok"] is True
    demonstration_id = created.json()["demonstration"]["id"]
    fetched = client.get(f"/api/teach/demonstrations/{demonstration_id}")
    assert fetched.status_code == 200
    assert fetched.json()["demonstration"]["id"] == demonstration_id


def test_immune_routes_preserve_uri_asset_ids(tmp_path, monkeypatch):
    service = ImmuneService(DurableFeatureStore(str(tmp_path / "immune.db")))
    client = _client(
        immune_routes, immune_routes.setup_immune_system_routes, "immune_service",
        service, monkeypatch,
    )
    asset_id = "tool://renderer"
    created = client.post(
        "/api/immune/assets",
        json={
            "asset_id": asset_id, "asset_version": "1.0.0", "kind": "tool",
            "health_contract": {"checks": ["smoke"], "ttl_seconds": 60},
        },
    )
    assert created.status_code == 200 and created.json()["ok"] is True
    fetched = client.get(f"/api/immune/assets/{quote(asset_id, safe='')}")
    assert fetched.status_code == 200
    assert fetched.json()["asset"]["id"] == asset_id


def test_branching_routes_create_and_read_a_future(tmp_path, monkeypatch):
    service = BranchingService(DurableFeatureStore(str(tmp_path / "futures.db")))
    client = _client(
        branching_routes, branching_routes.setup_branching_futures_routes,
        "branching_service", service, monkeypatch,
    )
    created = client.post(
        "/api/futures",
        json={
            "title": "Choose", "intent": "Compare implementations", "mode": "simulate",
            "strategies": [{"id": "small", "title": "Small"},
                           {"id": "fast", "title": "Fast"}],
        },
    )
    assert created.status_code == 200 and created.json()["ok"] is True
    future_id = created.json()["future"]["id"]
    fetched = client.get(f"/api/futures/{future_id}")
    assert fetched.status_code == 200
    assert len(fetched.json()["future"]["branches"]) == 2
