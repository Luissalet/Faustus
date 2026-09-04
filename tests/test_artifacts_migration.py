"""The artifacts table (core/database) — additive, idempotent, reversible.

Three properties, and the third is the one the masterplan actually asked for:

* additive — the migration adds one table and edits none, so the bridge back to
  the gallery lives in `legacy_gallery_id` rather than in a new column on
  `gallery_images`;
* idempotent — running the backfill twice imports nothing the second time;
* reversible — `rollback_artifacts_table()` puts the schema back, and the
  gallery it was built from is untouched.

The fourth thing checked here is the one that would be tempting to skip: an
image imported from the old gallery has no run, no backend and no recipe, and
the row says so instead of filling them in with today's defaults.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ArtifactRow, Base, GalleryImage
from src import contracts as C


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    """A database of this file's own.

    The shared in-memory database the suite defaults to is not safe ground for
    a migration test: other modules truncate the gallery, and a backfill that
    finds nothing then passes its own assertions for the wrong reason. This
    file ran green alone and red in the full suite until it got its own file.
    """
    url = "sqlite:///" + (tmp_path / "migration.db").as_posix()
    own_engine = create_engine(url, connect_args={"check_same_thread": False})
    own_sessions = sessionmaker(autocommit=False, autoflush=False, bind=own_engine)
    monkeypatch.setattr(db_mod, "engine", own_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", own_sessions)
    Base.metadata.create_all(bind=own_engine)
    yield own_engine
    own_engine.dispose()


@pytest.fixture()
def gallery(own_database):
    """Four images: two importable, two that must be refused with a reason."""
    SessionLocal = db_mod.SessionLocal
    tag = uuid.uuid4().hex[:8]
    rows = [
        GalleryImage(id=f"{tag}-a", filename=f"{tag}a.png", prompt="a cat",
                     model="sdxl", owner="luis", file_hash="a" * 64, file_size=1234),
        GalleryImage(id=f"{tag}-b", filename=f"{tag}b.mp4", prompt="a clip",
                     model=None, owner="luis"),
        GalleryImage(id=f"{tag}-c", filename=f"{tag}c.txt", prompt="not media",
                     owner="luis"),
        GalleryImage(id=f"{tag}-d", filename=f"sub/{tag}d.png", prompt="path",
                     owner="luis"),
    ]
    db = SessionLocal()
    try:
        for row in rows:
            db.add(row)
        db.commit()
    finally:
        db.close()
    return tag


def _rows(tag):
    db = db_mod.SessionLocal()
    try:
        return {r.legacy_gallery_id: r for r in db.query(ArtifactRow).filter(
            ArtifactRow.legacy_gallery_id.like(f"{tag}-%")).all()}
    finally:
        db.close()


def test_the_backfill_imports_what_it_understands_and_names_what_it_skips(gallery):
    report = db_mod._backfill_artifacts_from_gallery()
    assert report == {"created": 2, "skipped": 2,
                      "reasons": {"unknown_extension:txt": 1, "filename_is_a_path": 1}}
    assert "unknown_extension:txt" in report["reasons"]
    assert "filename_is_a_path" in report["reasons"]

    got = _rows(gallery)
    assert got[f"{gallery}-a"].kind == "image"
    assert got[f"{gallery}-b"].kind == "video"
    assert f"{gallery}-c" not in got and f"{gallery}-d" not in got


def test_running_it_twice_imports_nothing_the_second_time(gallery):
    first = db_mod._backfill_artifacts_from_gallery()
    assert first["created"] == 2
    second = db_mod._backfill_artifacts_from_gallery()
    assert second["created"] == 0
    assert len(_rows(gallery)) == 2


def test_an_imported_image_admits_what_it_never_knew(gallery):
    db_mod._backfill_artifacts_from_gallery()
    row = _rows(gallery)[f"{gallery}-a"]
    artifact = C.Artifact.parse({
        "id": row.id, "kind": row.kind, "filename": row.filename,
        "sha256": row.sha256 or "", "owner": row.owner or "",
        "provenance": {"model": row.model, "note": row.provenance_note},
    })
    gaps = artifact.provenance_gaps()
    assert "backend" in gaps and "recipe" in gaps and "run_id" in gaps
    assert "model" not in gaps          # this one the gallery did record
    assert row.backend is None and row.recipe is None
    assert "never recorded" in row.provenance_note


def test_a_video_with_no_model_says_unknown_rather_than_guessing(gallery):
    db_mod._backfill_artifacts_from_gallery()
    row = _rows(gallery)[f"{gallery}-b"]
    assert row.model is None
    assert row.sha256 is None            # the gallery had no hash for it


def test_the_migration_edits_no_existing_table(own_database):
    """The reason the reverse is a one-liner: `gallery_images` never learned a
    new column, so undoing this does not depend on which SQLite the user has."""
    from sqlalchemy import inspect
    columns = {c["name"] for c in inspect(own_database).get_columns("gallery_images")}
    assert "artifact_id" not in columns
    assert not any(name.startswith("artifact") for name in columns)


def test_rollback_drops_the_table_and_leaves_the_gallery_alone(own_database, gallery):
    from sqlalchemy import inspect
    db_mod._backfill_artifacts_from_gallery()
    assert "artifacts" in inspect(own_database).get_table_names()

    db_mod.rollback_artifacts_table()
    assert "artifacts" not in inspect(own_database).get_table_names()

    db = db_mod.SessionLocal()
    try:
        survivors = db.query(GalleryImage).filter(
            GalleryImage.id.like(f"{gallery}-%")).count()
    finally:
        db.close()
    assert survivors == 4
