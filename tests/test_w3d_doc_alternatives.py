"""tests/test_w3d_doc_alternatives.py — W3-D (CONTRATO_W3.md), CMP-13 follow-up.

`src/alternatives.py`'s `doc_version` isolation used to store its content
ONLY on the experiment's own JSON record — `docs/adaptations/decisions/
CMP-13.md` names this explicitly as a scope stop: "`doc_version` cableado al
almacén real de documentos de Studio | ausente, alcance declarado". This
exercises the wiring: `create_doc_experiment(..., document_id=...)` reads a
real `core.database.Document`'s live content, and `apply_alternative`
writes the merged result back as a real, provenance-tagged
`DocumentVersion` — never a second source of truth for the document.

Run: python3 -m pytest tests/test_w3d_doc_alternatives.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.database as cdb  # noqa: E402
from core.database import Document  # noqa: E402
from src import alternatives  # noqa: E402
from src import constants as constants_mod  # noqa: E402

OWNER = "luis"
OTHER = "mallory"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """Same sandbox `test_cmp13_alternatives.py` uses for the experiment's
    own JSON storage — `DATA_DIR` is read lazily, so patching the attribute
    on `src.constants` is enough."""
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    yield


@pytest.fixture()
def db(monkeypatch):
    """A real, disposable sqlite `Document`/`DocumentVersion` store —
    `src.alternatives` reaches it via `core.database.get_db_session()`,
    which reads the module-level `SessionLocal` at call time, so patching
    that attribute (not a pre-imported copy) is enough to sandbox it."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", session_factory)
    return session_factory


def _make_document(db, *, owner=OWNER, content="base text\n"):
    doc_id = str(uuid.uuid4())
    session = db()
    try:
        session.add(Document(id=doc_id, owner=owner, title="Doc", current_content=content, version_count=1))
        session.commit()
    finally:
        session.close()
    return doc_id


def _read_document(db, doc_id):
    session = db()
    try:
        return session.query(Document).filter(Document.id == doc_id).first()
    finally:
        session.close()


def _versions(db, doc_id):
    from core.database import DocumentVersion
    session = db()
    try:
        return (session.query(DocumentVersion)
                .filter(DocumentVersion.document_id == doc_id)
                .order_by(DocumentVersion.version_number).all())
    finally:
        session.close()


# ---------------------------------------------------------------------------
# create_doc_experiment(document_id=...): live content, not a stale copy
# ---------------------------------------------------------------------------
def test_create_doc_experiment_with_document_id_uses_live_content(db):
    doc_id = _make_document(db, content="the real live content\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "stale caller-supplied text\n",
                                              document_id=doc_id)
    assert exp["document_id"] == doc_id
    assert alternatives.base_doc_content(exp) == "the real live content\n"


def test_create_doc_experiment_without_document_id_is_unchanged(db):
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "plain text\n")
    assert exp["document_id"] is None
    assert alternatives.base_doc_content(exp) == "plain text\n"


def test_create_doc_experiment_rejects_document_of_another_owner(db):
    doc_id = _make_document(db, owner=OTHER, content="not yours\n")
    with pytest.raises(alternatives.AlternativesError) as exc:
        alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    assert exc.value.error_class == "alternatives.document_not_found"


# ---------------------------------------------------------------------------
# apply_alternative: the decisive wiring — a real DocumentVersion, tagged
# ---------------------------------------------------------------------------
def test_apply_alternative_writes_real_document_version(db):
    doc_id = _make_document(db, content="base text\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    alt = alternatives.add_alternative(OWNER, exp["id"], "my fix", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "edited text\n")

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert result["content"] == "edited text\n"
    assert result["document_version"]["unchanged"] is False
    assert result["document_version"]["version_number"] == 2

    doc = _read_document(db, doc_id)
    assert doc.current_content == "edited text\n"
    assert doc.version_count == 2

    versions = _versions(db, doc_id)
    assert len(versions) == 1  # the original row is Document.current_content itself, no version #1 stored here
    assert versions[0].version_number == 2
    assert versions[0].content == "edited text\n"
    assert versions[0].source == f"alternative:{exp['id']}"
    assert alt["id"] in versions[0].summary
    assert "my fix" in versions[0].summary


def test_apply_alternative_defaults_mine_to_live_document_content(db):
    """No `mine_doc_content` passed: apply must merge against what the
    document ACTUALLY holds right now, not the experiment's creation-time
    snapshot — the same "mine is always current" rule the filesystem
    isolations already follow."""
    doc_id = _make_document(db, content="line one\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "line one\nline two\n")

    # The user has not touched the document since experiment start -- a
    # clean fast-forward, resolved by reading the LIVE document, not a
    # value the caller had to remember to pass in.
    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert result["content"] == "line one\nline two\n"
    assert _read_document(db, doc_id).current_content == "line one\nline two\n"


def test_apply_alternative_conflict_writes_nothing_to_the_document(db):
    """A real three-way conflict (both the main document AND the
    alternative changed the same line differently since the experiment
    started) must abort BEFORE anything is written — including the real
    document, not just the experiment's own JSON record."""
    doc_id = _make_document(db, content="shared line\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "shared line FROM ALTERNATIVE\n")

    # The user keeps editing the main document after the experiment/alt
    # were created, on the SAME line, differently.
    session = db()
    try:
        row = session.query(Document).filter(Document.id == doc_id).first()
        row.current_content = "shared line FROM USER\n"
        row.version_count = 2
        session.commit()
    finally:
        session.close()

    with pytest.raises(alternatives.ApplyConflictError):
        alternatives.apply_alternative(OWNER, exp["id"], alt["id"])

    doc = _read_document(db, doc_id)
    assert doc.current_content == "shared line FROM USER\n"  # untouched
    assert doc.version_count == 2  # no phantom version created
    assert _versions(db, doc_id) == []


def test_apply_alternative_unchanged_content_writes_no_version(db):
    """`theirs == mine == base`: `_plan_merge`'s own "skip" branch, nothing
    touched anywhere -- including the real document, no phantom version."""
    doc_id = _make_document(db, content="same\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "same\n")

    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert result["skipped_same"] is True
    assert "document_version" not in result
    doc = _read_document(db, doc_id)
    assert doc.version_count == 1
    assert _versions(db, doc_id) == []


def test_apply_alternative_without_document_id_never_touches_the_db(db):
    """Back-compat: an experiment created WITHOUT `document_id` (today's
    existing callers/tests) must behave byte-for-byte as before — no
    document row exists to write to, and nothing raises trying."""
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "base text\n")
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "edited text\n")
    result = alternatives.apply_alternative(OWNER, exp["id"], alt["id"], mine_doc_content="base text\n")
    assert result["content"] == "edited text\n"
    assert "document_version" not in result


def test_apply_alternative_document_deleted_since_creation_raises(db):
    doc_id = _make_document(db, content="base\n")
    exp = alternatives.create_doc_experiment(OWNER, "proj1", "goal", "", document_id=doc_id)
    alt = alternatives.add_alternative(OWNER, exp["id"], "one", isolation="doc_version")
    alternatives.set_doc_version_content(OWNER, exp["id"], alt["id"], "changed\n")

    session = db()
    try:
        session.query(Document).filter(Document.id == doc_id).delete()
        session.commit()
    finally:
        session.close()

    with pytest.raises(alternatives.AlternativesError) as exc:
        alternatives.apply_alternative(OWNER, exp["id"], alt["id"])
    assert exc.value.error_class == "alternatives.document_not_found"


# ---------------------------------------------------------------------------
# W3-INT (CONTRATO_CMP_W2.md § W2-B/routes wiring): `POST /doc` now accepts
# `document_id` and threads it to `create_doc_experiment` — the "punto de
# cableado pendiente en las rutas" `docs/api/alternatives.md` names. Through
# a real TestClient against the actual router, since this crosses the HTTP
# boundary the module-level tests above don't (see e.g.
# tests/test_l82_git_create_repo.py for the same client-fixture shape used
# against `routes/git_routes.py`, which `alternatives_routes.py`'s own
# docstring says its gating follows).
# ---------------------------------------------------------------------------
@pytest.fixture()
def project_store(tmp_path, monkeypatch):
    from services import projects as projects_mod
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(db, project_store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import alternatives_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")

    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or None
    monkeypatch.setattr(alternatives_routes, "effective_user", _owner_from_header)
    monkeypatch.setattr(alternatives_routes, "get_project_store", lambda: project_store)

    app = FastAPI()
    app.include_router(alternatives_routes.setup_alternatives_routes())
    return TestClient(app)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


def test_post_doc_route_passes_document_id_through_to_live_content(client, project_store, db):
    project = project_store.create(name="AltProj", folder="AltProj", workspace="", owner=OWNER,
                                    scaffold_memory=False)
    doc_id = _make_document(db, content="the real live content\n")

    resp = client.post(
        f"/api/projects/{project['id']}/alternatives/doc",
        json={"goal": "goal", "base_content": "stale caller-supplied text\n", "document_id": doc_id},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_id"] == doc_id
    assert alternatives.base_doc_content(body) == "the real live content\n"


def test_post_doc_route_without_document_id_is_unchanged(client, project_store):
    project = project_store.create(name="AltProj2", folder="AltProj2", workspace="", owner=OWNER,
                                    scaffold_memory=False)
    resp = client.post(
        f"/api/projects/{project['id']}/alternatives/doc",
        json={"goal": "goal", "base_content": "plain text\n"},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_id"] is None
    assert alternatives.base_doc_content(body) == "plain text\n"
