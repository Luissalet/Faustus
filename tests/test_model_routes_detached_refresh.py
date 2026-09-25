"""/api/models after the inline refresh of a stale local endpoint, on a REAL
SQLAlchemy session: the refresh commits, a commit expires every loaded row,
and the listing used to read `ep.base_url` after closing the session —
DetachedInstanceError, HTTP 500 — whenever a local endpoint had aged out."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import ModelEndpoint
from routes import model_routes


class _NoThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


def _route(router, path):
    for r in router.routes:
        if getattr(r, "path", "") == path and "GET" in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


def test_listing_survives_the_commit_of_an_inline_local_refresh(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ModelEndpoint.__table__.create(engine)
    Session = sessionmaker(bind=engine)  # expire_on_commit=True, like the app
    old = datetime.fromtimestamp(time.time() - 11 * 86400, tz=timezone.utc)
    with Session() as s:
        for i, base in enumerate(("http://127.0.0.1:11434/v1", "http://127.0.0.1:8081/v1")):
            s.add(ModelEndpoint(id=f"ep{i}", name=f"local{i}", base_url=base, is_enabled=True,
                                cached_models=json.dumps(["old-model"]), endpoint_kind="local",
                                model_refresh_mode="auto", updated_at=old, created_at=old))
        s.commit()

    router = model_routes.setup_model_routes(model_discovery=None)
    monkeypatch.setattr(model_routes, "SessionLocal", Session)
    monkeypatch.setattr(model_routes, "_auth_disabled", lambda: True)
    monkeypatch.setattr(threading, "Thread", _NoThread)
    # First endpoint refreshes (commit), second fails (no commit) -- both
    # rows are expired by that commit and must still be readable.
    monkeypatch.setattr(model_routes, "_probe_endpoint",
                        lambda base, key=None, timeout=None: ["new-model"] if "11434" in base else None)
    req = SimpleNamespace(state=SimpleNamespace(current_user=None),
                          app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)))
    result = _route(router, "/api/models")(req)
    items = {it["endpoint_id"]: it for it in result["items"]}
    assert items["ep0"]["models"] == ["new-model"]
    assert items["ep1"]["models"] == ["old-model"]
