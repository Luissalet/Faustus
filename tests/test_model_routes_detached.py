"""GET /api/models against a real SQLAlchemy session: a stale local endpoint
refreshed inline commits, which expires every loaded row; the listing that
follows must not read them detached (seen live: the first /api/models after a
restart answered 500 and the picker showed "No models")."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.model_routes as model_routes
from core.database import ModelEndpoint


class _NoopThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


def _request():
    return SimpleNamespace(state=SimpleNamespace(current_user=None),
                           app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)))


def _endpoint(router, path):
    for route in router.routes:
        if getattr(route, "path", "") == path and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(path)


def test_inline_refresh_then_listing_reads_live_rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    ModelEndpoint.__table__.create(engine)
    Session = sessionmaker(bind=engine)  # expire_on_commit=True, like the app
    stale = datetime.fromtimestamp(time.time() - 11 * 86400, tz=timezone.utc)
    with Session() as db:
        db.add(ModelEndpoint(id="ollama", name="ollama", base_url="http://127.0.0.1:11434/v1",
                             is_enabled=True, cached_models=json.dumps(["old:1b"]),
                             endpoint_kind="local", model_refresh_mode="auto", model_type="llm",
                             updated_at=stale))
        db.add(ModelEndpoint(id="llama", name="llama.cpp (local)", base_url="http://127.0.0.1:8081/v1",
                             is_enabled=True, cached_models=json.dumps(["qwen-27b"]),
                             endpoint_kind="local", model_refresh_mode="auto", model_type="llm",
                             updated_at=stale))
        db.commit()

    router = model_routes.setup_model_routes(model_discovery=None)
    monkeypatch.setattr(model_routes, "SessionLocal", Session)
    monkeypatch.setattr(model_routes, "_auth_disabled", lambda: True)
    monkeypatch.setattr(threading, "Thread", _NoopThread)
    monkeypatch.setattr(model_routes, "_probe_endpoint",
                        lambda base, api_key=None, timeout=None:
                        ["new:8b"] if "11434" in base else None)

    result = _endpoint(router, "/api/models")(_request(), refresh=False, background=False)

    by_id = {item["endpoint_id"]: item for item in result["items"]}
    assert by_id["ollama"]["models"] == ["new:8b"]
    assert by_id["llama"]["models"] == ["qwen-27b"]
