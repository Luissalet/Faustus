"""SEC-04 · GET/PUT /api/privacy/profile — the admin surface for the profile
`src/privacy_policy.py::assert_outbound` already enforces.

Before this there was no way to see or change the active `local_only` /
`local_preferred` / `cloud_allowed` profile short of hand-editing
data/settings.json — the gate existed and was wired into several auxiliaries
(embedding lane, ChromaDB, the compaction summarizer, the reranker), but
nothing let a person point at it.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.privacy_routes import setup_privacy_routes
from src import privacy_policy
from src import settings as settings_module


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(path))
    settings_module._invalidate_caches()
    monkeypatch.setattr(settings_module, "_CACHE_TTL", 0.0)
    yield path
    settings_module._invalidate_caches()


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_privacy_routes())
    return TestClient(app)


def test_get_defaults_to_local_preferred_when_never_set(client):
    r = client.get("/api/privacy/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"] == privacy_policy.PROFILE_LOCAL_PREFERRED
    assert set(body["profiles"]) == set(privacy_policy.PROFILES)
    assert body["is_explicit"] is False


def test_put_persists_and_get_reflects_it(client):
    r = client.put("/api/privacy/profile", json={"profile": "local_only"})
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == "local_only"

    again = client.get("/api/privacy/profile")
    assert again.json()["profile"] == "local_only"
    assert again.json()["is_explicit"] is True

    # And the gate itself now reads the persisted value.
    assert privacy_policy.get_privacy_profile() == "local_only"


def test_put_rejects_an_unknown_profile(client):
    r = client.put("/api/privacy/profile", json={"profile": "definitely_not_a_profile"})
    assert r.status_code == 422  # pydantic field validator


def test_persisted_profile_survives_a_settings_reload(client, store):
    client.put("/api/privacy/profile", json={"profile": "cloud_allowed"})
    settings_module._invalidate_caches()
    assert settings_module.get_setting(privacy_policy.SETTING_KEY) == "cloud_allowed"


def test_route_requires_admin(client, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    r = client.get("/api/privacy/profile")
    assert r.status_code in (401, 403)
    w = client.put("/api/privacy/profile", json={"profile": "local_only"})
    assert w.status_code in (401, 403)
