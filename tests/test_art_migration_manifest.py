"""tests/test_art_migration_manifest.py — backfilling manifests (ART-01).

`src.artifact_migration.migrate_manifests()` gives every pre-existing
occurrence (including the ones `copy_legacy()` already produced, since that
copy predates the manifest table) a version-1 manifest. Three properties to
check, matching the batch's hard rules: additive (nothing existing is
touched), idempotent (a second pass creates nothing new), and `dry_run` is
real (a dry run writes no row at all, not even a version 1 that then gets
"already there" on the next honest pass).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import artifact_identity as identity
from src import artifact_migration
from src.contracts.blob import ArtifactOccurrence


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "migrate_manifests.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _occurrences(n, own_database):
    ids = []
    for i in range(n):
        digest = format(i + 1, "064x")  # a distinct, valid-looking sha256 per row
        identity.ensure_blob(sha256=digest, byte_size=10, filename=f"{digest}.txt",
                             media_type="text/plain")
        occ_id = f"occ_backfill_{i}"
        identity.ensure_occurrence(ArtifactOccurrence.parse({
            "id": occ_id, "blob_sha256": digest, "kind": "text", "owner": "luis",
        }))
        ids.append(occ_id)
    return ids


def test_dry_run_writes_nothing(own_database):
    ids = _occurrences(3, own_database)
    report = artifact_migration.migrate_manifests(dry_run=True)
    assert report["would_create"] == 3
    assert report["created"] == 0
    for occ_id in ids:
        assert identity.latest_manifest(occ_id) is None, \
            "dry_run must not write a manifest row"


def test_a_real_pass_creates_one_generated_manifest_per_occurrence(own_database):
    ids = _occurrences(3, own_database)
    report = artifact_migration.migrate_manifests(dry_run=False)
    assert report["created"] == 3
    for occ_id in ids:
        manifest = identity.latest_manifest(occ_id)
        assert manifest is not None
        assert manifest["state"] == "generated"
        assert manifest["version"] == 1
        assert "backfilled" in manifest["reason"]


def test_running_it_twice_creates_nothing_the_second_time(own_database):
    _occurrences(3, own_database)
    first = artifact_migration.migrate_manifests(dry_run=False)
    assert first["created"] == 3
    second = artifact_migration.migrate_manifests(dry_run=False)
    assert second["created"] == 0
    assert second["already_there"] == 3


def test_an_occurrence_with_a_manifest_already_is_skipped_not_duplicated(own_database):
    ids = _occurrences(1, own_database)
    identity.record_manifest_version(ids[0], state="generated")
    identity.transition_state(ids[0], "validated")
    report = artifact_migration.migrate_manifests(dry_run=False)
    assert report["already_there"] == 1
    assert report["created"] == 0
    # The manifest the caller made is untouched, not replaced by a backfilled one.
    assert identity.latest_manifest(ids[0])["state"] == "validated"
