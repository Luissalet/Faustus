"""ART-05 — reproducible analysis: a saved script and its input data are
hashed at collection time (`artifact_store.analysis_provenance`) and
readable back through the artifact (GET .../provenance), so a numeric claim
can be linked to the exact script and bytes that produced it rather than
taken on faith. Sandboxed execution itself is EXEC-01's authority, not
reimplemented here — only the provenance record is.
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store
from src.contracts import ExecutionResult


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "prov.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    db_mod.Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def test_analysis_provenance_hashes_script_and_inputs(tmp_path):
    script = "print(sum(range(10)))\n"
    data_file = tmp_path / "sales.csv"
    data_file.write_bytes(b"month,total\njan,100\n")

    prov = artifact_store.analysis_provenance(
        script_text=script, input_paths=[str(data_file)], engine="python", recipe="monthly_total")

    assert prov["recipe_fingerprint"] == hashlib.sha256(script.encode("utf-8")).hexdigest()
    assert prov["inputs_digest"] is not None
    assert prov["backend"] == "python"
    assert prov["recipe"] == "monthly_total"
    assert prov["recipe_fingerprint"][:12] in prov["note"]


def test_analysis_provenance_is_deterministic_for_identical_inputs(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    p1 = artifact_store.analysis_provenance(script_text="s", input_paths=[str(a)])
    p2 = artifact_store.analysis_provenance(script_text="s", input_paths=[str(b)])
    assert p1["inputs_digest"] == p2["inputs_digest"]  # same bytes -> same digest, regardless of path


def test_analysis_provenance_flags_unreadable_inputs_instead_of_silently_dropping(tmp_path):
    missing = str(tmp_path / "does-not-exist.csv")
    prov = artifact_store.analysis_provenance(script_text="s", input_paths=[missing])
    assert prov["inputs_digest"] is None
    assert "unreadable" in prov["note"]


def test_provenance_survives_collect_and_is_readable_via_the_artifact(own_database, tmp_path):
    """End to end: an analysis run collects a result with `analysis_provenance`,
    and the provenance route surfaces the same script/inputs hashes back."""
    script = "SELECT SUM(total) FROM sales;"
    data_file = tmp_path / "sales.csv"
    data_file.write_bytes(b"month,total\njan,100\n")
    prov = artifact_store.analysis_provenance(
        script_text=script, input_paths=[str(data_file)], engine="sql", recipe="monthly_total")

    src_dir = tmp_path / "run"
    src_dir.mkdir()
    (src_dir / "result.json").write_text('{"total": 100}')
    result = ExecutionResult.parse({
        "run_id": "r1", "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": ["result.json"],
    })
    collected = artifact_store.collect(result, source_dir=str(src_dir), owner="alice",
                                       project_id="p1", provenance=prov)
    artifact_store.persist(collected.artifacts, session_id="s1")
    artifact_id = collected.artifacts[0].id

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import artifact_routes as routes
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(routes, "_owner", lambda request: "alice")
    app = FastAPI()
    app.include_router(routes.setup_artifact_routes())
    with TestClient(app) as client:
        resp = client.get(f"/api/artifacts/{artifact_id}/provenance")
        assert resp.status_code == 200, resp.text
        body = resp.json()["provenance"]
        assert body["recipe_fingerprint"] == prov["recipe_fingerprint"]
        assert body["inputs_digest"] == prov["inputs_digest"]
        assert body["recipe"] == "monthly_total"
        assert body["backend"] == "sql"

        forbidden_owner = TestClient(app)
        monkeypatch.setattr(routes, "_owner", lambda request: "mallory")
        resp2 = client.get(f"/api/artifacts/{artifact_id}/provenance")
        assert resp2.status_code == 404
    monkeypatch.undo()
