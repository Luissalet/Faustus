"""QA-28/MOD-06 · switching a session's model mid-task must say what was lost.

`PATCH /session/{sid}` with a new `model`/`endpoint_url` used to swap the
session's route and stop there — nothing told the caller (Studio, or an
agent loop reading its own turn result) that the new model has no native
tool-calling or vision even though the model it just left did. Lote 20
(closing L17/MOD-06) makes the endpoint recompute both models' manifests
from the existing `src/model_calibration.py` store (never a live probe —
this endpoint has no business loading a model) and report `capabilities`
(the new model's manifest) and `lost` (labels present on the previous
model's announced capabilities but absent on the new one).

Reuses `tests/test_archived_sessions_model_filter.py`'s pattern: call the
route function directly against a real (temp-file) sqlite DB, since this is
a single endpoint and never crosses an HTTP boundary into another route.
"""
import sys
import tempfile
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Session as DbSession
from src import model_calibration as mcal

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _route(router, path, method="PATCH"):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {path}")


def _stub_multipart_if_missing(monkeypatch):
    """setup_session_routes() registers Form()-based routes; FastAPI probes
    for python-multipart at registration time even though this test never
    parses a real multipart body."""
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


@pytest.fixture
def rename_endpoint(monkeypatch, tmp_path):
    import routes.session_routes as sr

    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setattr(sr, "SessionLocal", _TS)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    # Not under test here: a raw endpoint_url for a non-admin is normally
    # rejected unless a registered endpoint_id is given.
    monkeypatch.setattr(sr, "_reject_raw_endpoint_url_for_non_admin", lambda *a, **k: None)
    # Isolate the manifest store this test reads/writes from the real
    # DATA_DIR — model_calibration.py's own store, not a parallel one.
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))

    session_manager = MagicMock()
    fake_session = SimpleNamespace(
        model="old-vision-model", endpoint_url="https://old-endpoint.example.com", headers={}, folder=None,
    )
    session_manager.get_session.return_value = fake_session
    router = sr.setup_session_routes(session_manager, {})
    endpoint = _route(router, "/api/session/{sid}")

    db = _TS()
    sid = f"sess-{uuid.uuid4()}"
    try:
        db.add(DbSession(id=sid, owner="alice", name="chat", endpoint_url="https://old-endpoint.example.com",
                          model="old-vision-model", headers={}))
        db.commit()
    finally:
        db.close()
    return endpoint, sid, fake_session


def _switch(endpoint, sid, model, endpoint_url):
    return endpoint(request=None, sid=sid, name=None, folder=None,
                     model=model, endpoint_url=endpoint_url, endpoint_id=None)


def test_switching_to_a_model_without_vision_or_tools_reports_what_was_lost(rename_endpoint):
    endpoint, sid, _ = rename_endpoint
    mcal.save_announced(
        mcal.manifest_key(vendor="openai", model_id="old-vision-model"),
        {"capabilities": {"vision": True, "tools": True}},
    )
    # The new model has no manifest at all yet — an honest "nothing known",
    # not a guess from its name.

    result = _switch(endpoint, sid, "new-text-model", "https://new-endpoint.example.com")

    assert result["model"] == "new-text-model"
    assert "capabilities" in result
    assert set(result["lost"]) == {"vision", "native tool calling"}


def test_switching_between_two_models_with_no_manifest_reports_nothing_lost(rename_endpoint):
    """Honest-empty path: when neither side has been calibrated/announced,
    `lost` must not fabricate a guess — it stays empty."""
    endpoint, sid, _ = rename_endpoint

    result = _switch(endpoint, sid, "some-other-model", "https://another-endpoint.example.com")

    assert result["lost"] == []
    assert result["capabilities"] == {"announced": {}, "tested": {}, "degraded": [], "updated_at": ""}
