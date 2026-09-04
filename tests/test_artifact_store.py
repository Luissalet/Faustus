"""The artifact store (src/artifact_store) — what the run left, kept honestly.

Four properties, and three of them are refusals to be clever:

* only the files the *result* names are collected, so a run is never credited
  with a neighbour's output;
* a type that cannot be inferred is `binary`, not a guess — the alternative is
  dropping the user's bytes or writing a kind into an audit table that nothing
  verified;
* identical bytes are one file, and re-collecting them creates no second row;
* a name that is not a bare name never reaches the filesystem.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ArtifactRow, Base
from src import artifact_store
from src.contracts import ExecutionResult


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    """Its own file, for the same reason the migration test has one: the
    shared in-memory database is not ground a persistence test can stand on."""
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
    (src / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (src / "mystery.qqq").write_bytes(b"\x00\x01raw")
    (src / "not_mine.txt").write_text("written by an earlier run", encoding="utf-8")
    return {"dir": str(src), "store": str(tmp_path / "store")}


def _result(names, **over):
    body = {"run_id": "run-1", "backend": "docker_workspace", "status": "completed",
            "exit_code": 0, "artifact_filenames": list(names)}
    body.update(over)
    return ExecutionResult.parse(body)


def test_only_what_the_result_names_is_collected(run_output):
    collected = artifact_store.collect(
        _result(["report.md", "data.csv", "mystery.qqq"]),
        source_dir=run_output["dir"], store_dir=run_output["store"])
    assert {a.label for a in collected.artifacts} == {"report.md", "data.csv", "mystery.qqq"}
    # The file this run did not write is still sitting where it was.
    assert os.path.exists(os.path.join(run_output["dir"], "not_mine.txt"))


def test_a_type_it_cannot_infer_is_binary_and_not_a_guess(run_output):
    kinds = {a.label: a.kind for a in artifact_store.collect(
        _result(["report.md", "data.csv", "mystery.qqq"]),
        source_dir=run_output["dir"], store_dir=run_output["store"]).artifacts}
    assert kinds == {"report.md": "document", "data.csv": "dataset",
                     "mystery.qqq": "binary"}
    assert artifact_store.kind_of("clip.mp4") == "video"
    assert artifact_store.kind_of("noextension") == "binary"


def test_the_stored_name_is_the_content_hash_so_a_run_cannot_overwrite_another(run_output):
    collected = artifact_store.collect(
        _result(["report.md"]), source_dir=run_output["dir"], store_dir=run_output["store"])
    art = collected.artifacts[0]
    assert art.filename == f"{art.sha256}.md"
    assert art.label == "report.md"            # the name the run chose survives
    assert os.path.exists(os.path.join(run_output["store"], art.filename))


def test_identical_bytes_are_one_file_and_one_row(run_output, own_database, tmp_path):
    first = artifact_store.collect(_result(["data.csv"]), source_dir=run_output["dir"],
                                   store_dir=run_output["store"])
    assert artifact_store.persist(first.artifacts) == {"created": 1, "already_there": 0}

    second_dir = tmp_path / "run-2"
    second_dir.mkdir()
    (second_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    second = artifact_store.collect(
        _result(["data.csv"], run_id="run-2"),
        source_dir=str(second_dir), store_dir=run_output["store"])

    assert second.deduplicated == 1
    assert len(os.listdir(run_output["store"])) == 1
    assert artifact_store.persist(second.artifacts) == {"created": 0, "already_there": 1}


def test_a_vanished_or_escaping_name_is_skipped_with_a_reason(run_output):
    collected = artifact_store.collect(
        _result(["report.md", "gone.txt", "sub/../../etc/passwd"]),
        source_dir=run_output["dir"], store_dir=run_output["store"])
    reasons = {s["name"]: s["reason"] for s in collected.skipped}
    assert reasons["gone.txt"] == "vanished_before_collection"
    assert reasons["sub/../../etc/passwd"] == "not_a_bare_name"
    assert [a.label for a in collected.artifacts] == ["report.md"]


def test_a_partial_run_keeps_its_output_and_says_it_is_partial(run_output, own_database):
    collected = artifact_store.collect(
        _result(["report.md"], status="timeout", exit_code=None,
                reason="killed after 3s", partial=True),
        source_dir=run_output["dir"], store_dir=run_output["store"])
    assert collected.artifacts[0].partial is True
    artifact_store.persist(collected.artifacts)
    db = db_mod.SessionLocal()
    try:
        assert db.query(ArtifactRow).one().partial is True
    finally:
        db.close()


def test_the_row_records_the_backend_and_leaves_what_it_cannot_know_null(run_output, own_database):
    collected = artifact_store.collect(
        _result(["report.md"]), source_dir=run_output["dir"], store_dir=run_output["store"],
        owner="luis", project_id="book", skill_id="document.report", skill_version="1.0.0",
        provenance={"recipe": "report.v1", "recipe_version": "1.0.0"})
    art = collected.artifacts[0]
    assert art.provenance.backend == "docker_workspace"
    assert art.provenance.model is None
    assert set(art.provenance_gaps()) == {"model", "inputs_digest"}

    artifact_store.persist(collected.artifacts, session_id="s1")
    db = db_mod.SessionLocal()
    try:
        row = db.query(ArtifactRow).one()
        assert (row.backend, row.recipe, row.owner) == ("docker_workspace", "report.v1", "luis")
        assert row.model is None and row.model_license is None and row.inputs_digest is None
        assert row.session_id == "s1"
    finally:
        db.close()


def test_a_name_that_is_not_a_bare_name_never_resolves(run_output):
    good = artifact_store.path_of("abc.md", store_dir=run_output["store"])
    assert good.endswith("abc.md")
    for escape in ("../secrets.txt", "sub/x.md", "", "a\\b.md"):
        with pytest.raises(ValueError):
            artifact_store.path_of(escape, store_dir=run_output["store"])


def test_a_file_over_the_cap_is_skipped_rather_than_hashed(run_output):
    collected = artifact_store.collect(
        _result(["report.md"]), source_dir=run_output["dir"],
        store_dir=run_output["store"], max_bytes=2)
    assert collected.artifacts == ()
    assert collected.skipped[0]["reason"] == "larger_than_2_bytes"
