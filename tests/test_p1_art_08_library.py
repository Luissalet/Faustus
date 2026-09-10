"""ART-08 — universal results Library: search across artifact kinds/dates/
sessions/projects, and deleting one context link without losing bytes another
occurrence still shares.

`src.artifact_catalog.recent()` (extended here) and `src.artifact_identity.
forget_occurrence()` (pre-existing, reused per COMUN.md rule 4) are the two
authorities this exercises — no second index or delete path is created.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_catalog, artifact_store
from src.contracts import ExecutionResult


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "library.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield engine
    engine.dispose()


def _make(tmp_path, run_id, filename, body, *, owner="alice", project_id="p1",
         session_id="s1"):
    src_dir = tmp_path / f"run-{run_id}"
    src_dir.mkdir(exist_ok=True)
    (src_dir / filename).write_bytes(body)
    result = ExecutionResult.parse({
        "run_id": run_id, "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": [filename],
    })
    collected = artifact_store.collect(result, source_dir=str(src_dir), owner=owner,
                                       project_id=project_id)
    artifact_store.persist(collected.artifacts, session_id=session_id)
    return collected.artifacts[0]


def test_recent_filters_by_kind_session_and_date_range(own_database, tmp_path):
    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    with db_mod.SessionLocal() as db:
        report = _make(tmp_path, "r1", "report.md", b"# hi\n", session_id="s1")
        dataset = _make(tmp_path, "r2", "data.csv", b"a,b\n1,2\n", session_id="s2")

        everything = artifact_catalog.recent(db, owner="alice")
        assert {a.id for a in everything} == {report.id, dataset.id}

        only_docs = artifact_catalog.recent(db, owner="alice", kind="document")
        assert [a.id for a in only_docs] == [report.id]

        only_s2 = artifact_catalog.recent(db, owner="alice", session_id="s2")
        assert [a.id for a in only_s2] == [dataset.id]

        by_label = artifact_catalog.recent(db, owner="alice", q="report")
        assert [a.id for a in by_label] == [report.id]

        # An impossible future window returns nothing — proves the date
        # filter actually narrows instead of being ignored.
        future_only = artifact_catalog.recent(db, owner="alice", since="2999-01-01")
        assert future_only == []
        with pytest.raises(ValueError):
            artifact_catalog.recent(db, owner="alice", since="not-a-date")


def test_list_artifacts_route_applies_kind_and_session_filters(own_database, tmp_path, monkeypatch):
    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    _make(tmp_path, "r1", "report.md", b"# hi\n", session_id="s1")
    _make(tmp_path, "r2", "data.csv", b"a,b\n1,2\n", session_id="s2")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import artifact_routes as routes

    monkeypatch.setattr(routes, "_owner", lambda request: "alice")
    app = FastAPI()
    app.include_router(routes.setup_artifact_routes())
    with TestClient(app) as client:
        resp = client.get("/api/artifacts", params={"kind": "dataset"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["artifacts"]) == 1
        assert body["artifacts"][0]["kind"] == "dataset"

        bad = client.get("/api/artifacts", params={"since": "garbage"})
        assert bad.status_code == 400


def test_forgetting_one_occurrence_keeps_bytes_shared_with_a_sibling(own_database, tmp_path, monkeypatch):
    """Two occurrences (two conversations) point at identical bytes — a run
    that produced the same report twice. Deleting the Library's link to ONE
    of them must not take the file out from under the other (ART-08's
    acceptance criterion)."""
    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    first = _make(tmp_path, "r1", "report.md", b"same bytes\n", session_id="s1")
    second = _make(tmp_path, "r2", "report.md", b"same bytes\n", session_id="s2")
    assert first.id != second.id  # two distinct occurrences ...
    with db_mod.SessionLocal() as db:
        rows = artifact_catalog.recent(db, owner="alice")
        assert {r.filename for r in rows} == {first.filename}  # ... one shared blob

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import artifact_routes as routes

    monkeypatch.setattr(routes, "_owner", lambda request: "alice")
    app = FastAPI()
    app.include_router(routes.setup_artifact_routes())
    with TestClient(app) as client:
        resp = client.delete(f"/api/artifacts/{first.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["references_left"] == 1

        # The deleted link is really gone (Library no longer lists it) ...
        still_listed = client.get("/api/artifacts").json()["artifacts"]
        assert first.id not in {a["id"] for a in still_listed}
        # ... but the surviving occurrence can still download the same bytes.
        alive = client.get(f"/api/artifacts/{second.id}/download")
        assert alive.status_code == 200
        assert alive.content == b"same bytes\n"

        gone = client.get(f"/api/artifacts/{first.id}")
        assert gone.status_code == 404
