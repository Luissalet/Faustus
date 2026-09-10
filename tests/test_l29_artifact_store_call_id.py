"""L29 (integrates L21's "necessary in src/artifact_store.py" note):
`persist()` gains an additive, optional `call_id` kwarg, folded into
`provenance.note` (`ArtifactOccurrenceRow.provenance_note`) — traceability
from a stored artifact back to the tool call that produced it.

Reverting the `call_id` handling in `persist()` makes `test_call_id_lands_in_the_stored_provenance_note`
fail: the row's `provenance_note` stays empty instead of carrying the tag.
`test_call_id_is_optional_and_leaves_provenance_note_untouched` pins the
back-compat side: omitting it must reproduce exactly what
`tests/test_artifact_store.py` already asserts.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ArtifactOccurrenceRow, Base
from src import artifact_store
from src.contracts import ExecutionResult


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "store.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def run_output(tmp_path):
    src = tmp_path / "run-1"
    src.mkdir()
    (src / "report.md").write_text("# Report\n", encoding="utf-8")
    return {"dir": str(src), "store": str(tmp_path / "store")}


def _result(names, **over):
    body = {"run_id": "run-1", "backend": "docker_workspace", "status": "completed",
            "exit_code": 0, "artifact_filenames": list(names)}
    body.update(over)
    return ExecutionResult.parse(body)


def test_call_id_lands_in_the_stored_provenance_note(run_output, own_database):
    collected = artifact_store.collect(
        _result(["report.md"]), source_dir=run_output["dir"], store_dir=run_output["store"])
    artifact_store.persist(collected.artifacts, call_id="call_abc123")
    db = db_mod.SessionLocal()
    try:
        row = db.query(ArtifactOccurrenceRow).one()
        assert "call_abc123" in (row.provenance_note or "")
    finally:
        db.close()


def test_call_id_is_optional_and_leaves_provenance_note_untouched(run_output, own_database):
    collected = artifact_store.collect(
        _result(["report.md"]), source_dir=run_output["dir"], store_dir=run_output["store"])
    artifact_store.persist(collected.artifacts)
    db = db_mod.SessionLocal()
    try:
        row = db.query(ArtifactOccurrenceRow).one()
        assert (row.provenance_note or "") == ""
    finally:
        db.close()
