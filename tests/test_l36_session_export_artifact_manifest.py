"""Lot 36, requirement 6: a session export used to hand back bytes that came
from nowhere and went nowhere else — no artifact row, no manifest, no
content-address. `routes/session_routes.py::export_session` now also runs
those bytes through `src.artifact_store.collect()` + `.persist()` (the same
authority `src/workflows/artifacts.py::save` already uses for workflow
outputs), so the export gets a real, queryable occurrence — QA-39 "for real"
this time.

This is additive and best-effort: the download response itself is built from
`result.content` exactly as before, unconditionally. A broken artifact store
must never turn a working export into a 500 — see
`test_a_broken_artifact_store_never_breaks_the_download` below.

Harness borrowed from `tests/test_session_export_routes.py` (same
`_make_export_double`/`_install_export_double`/`_temp_db` idiom) rather than
duplicated wholesale.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_session_export_routes import (
    _add_session,
    _install_export_double,
    _make_export_double,
    _Harness,
)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import routes.session_routes as sr
    from core import database as db_mod
    from src import artifact_store

    # Same own-database idiom as tests/qa/test_qa_39_export_defectuoso.py and
    # the other artifact-identity tests: `src.artifact_identity` imports
    # `core.database.SessionLocal` fresh on every call, so patching only the
    # route module's own reference (as test_session_export_routes.py does)
    # would leave the artifact-store side of this test talking to a
    # different database than the session/export side.
    engine = create_engine(f"sqlite:///{tmp_path / 'export.db'}",
                           connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(sr, "SessionLocal", factory)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")

    # A real artifact store on a throwaway directory — this lot's point is
    # that bytes actually reach it, so a mock would prove nothing.
    store_dir = tmp_path / "artifacts"
    store_dir.mkdir()
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(store_dir))

    export = _install_export_double(monkeypatch, _make_export_double())

    store = {}

    def get_session(sid):
        if sid not in store:
            raise KeyError(sid)
        return store[sid]

    sm = MagicMock()
    sm.sessions = store
    sm.get_session.side_effect = get_session

    router = sr.setup_session_routes(sm, {})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield _Harness(client, sm, factory, export)


def _occurrences_for(session_factory, session_id):
    from core.database import ArtifactOccurrenceRow
    db = session_factory()
    try:
        return db.query(ArtifactOccurrenceRow).filter(
            ArtifactOccurrenceRow.session_id == session_id).all()
    finally:
        db.close()


def test_export_is_collected_and_persisted_as_a_real_artifact(harness):
    from src import artifact_identity as identity

    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
    assert r.status_code == 200, r.text
    # The download itself is unaffected — same bytes as the double returns.
    assert r.content == b"<md:Roadmap>"

    matches = _occurrences_for(harness.db_factory, sid)
    assert len(matches) == 1
    row = matches[0]
    assert row.skill_id == "chat.export"
    assert row.owner == "alice"

    # The bytes are actually on disk, content-addressed, not just a row.
    from pathlib import Path
    stored_path = Path(identity.path_for(row.id, owner="alice"))
    assert stored_path.read_bytes() == b"<md:Roadmap>"


def test_two_exports_of_the_same_session_produce_two_distinct_occurrences(harness):
    """No capability lost: nothing about the response changes, and repeating
    an export is not an error — each call is its own run."""
    sid = _add_session(harness, name="Roadmap")
    for _ in range(2):
        r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
        assert r.status_code == 200

    rows = _occurrences_for(harness.db_factory, sid)
    assert len(rows) == 2
    assert rows[0].id != rows[1].id
    # Same bytes both times -> the underlying blob is deduplicated even
    # though there are two distinct occurrences (matches artifact_store's
    # own dedup contract, exercised elsewhere for other producers).
    assert rows[0].blob_sha256 == rows[1].blob_sha256


def test_a_broken_artifact_store_never_breaks_the_download(harness, monkeypatch):
    """The export contract (COMUN.md rule 3: no capability lost) must not
    start depending on the artifact store's health. This is the test that
    fails without the try/except around the new call."""
    def boom(*args, **kwargs):
        raise RuntimeError("disk is on fire")

    # _record_export_artifact is a closure defined inside
    # setup_session_routes(), not a module attribute — patch the
    # artifact_store call it makes instead, the actual failure surface named
    # in the code comment ("disk full, artifact store down, …").
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "collect", boom)

    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
    assert r.status_code == 200, r.text
    assert r.content == b"<md:Roadmap>"


def test_single_user_mode_with_no_owner_still_does_not_break_the_download(harness, monkeypatch):
    """`effective_user` can legitimately return "" (AUTH_ENABLED=false, no
    stamped user) — same normalization concern as requirement 1's owner=None
    bug, but here for the artifact_store owner field, which accepts ''."""
    import routes.session_routes as sr
    monkeypatch.setattr(sr, "effective_user", lambda request: "")
    monkeypatch.setattr(sr, "_auth_disabled", lambda: True)

    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
    assert r.status_code == 200, r.text
