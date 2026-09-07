"""Blobs, occurrences and derivatives (B-017) — bytes are not artifacts.

The bug: `src/artifact_store.py` names an artifact `art_{sha256[:24]}` and uses
that as the idempotency key, so the second producer of identical bytes is
discarded as "already there" along with its owner, run, label, provenance and
retention. `test_the_old_store_still_loses_the_second_occurrence` is the
regression witness for that, kept green on purpose: it documents what the
production write path still does until the cutover lands.

Everything else here is the store that does not do it, and it is B-017's own
mandatory list: same content from two runs, same content from two owners,
deleting one occurrence, garbage collection, and two writers racing on one
hash. Plus the property the whole split exists for — physical deduplication
grants no logical access.
"""
from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ArtifactOccurrenceRow, ArtifactRow, Base, BlobRow
from src import artifact_identity as ident
from src.contracts import ContractError
from src.contracts.blob import (
    ArtifactOccurrence, Blob, DerivedArtifact, blob_id_for, derived_id_for,
    new_occurrence_id,
)

BYTES = b"a,b\n1,2\n"
DIGEST = "9d8f1a1e7c0d1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70819200"
OTHER = "1111111111111111111111111111111111111111111111111111111111111111"


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    """Its own file, for the reason the other artifact tests give: the shared
    in-memory database is not ground a persistence test can stand on, and the
    race test below needs a real file two connections can contend for."""
    url = "sqlite:///" + (tmp_path / "identity.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def store(tmp_path):
    """A store directory with the blob's bytes already in it, named the way the
    existing store names them: `<sha256>.<ext>`. The split moves no files."""
    path = tmp_path / "store"
    path.mkdir()
    (path / f"{DIGEST}.csv").write_bytes(BYTES)
    return str(path)


def _blob(digest: str = DIGEST, size: int = len(BYTES), ext: str = "csv"):
    return ident.ensure_blob(sha256=digest, byte_size=size,
                             filename=f"{digest}.{ext}", media_type="text/csv")


def _occurrence(owner: str, run_id: str, *, digest: str = DIGEST, **over):
    body = {"id": new_occurrence_id(), "kind": "dataset", "blob_sha256": digest,
            "label": "data.csv", "owner": owner, "run_id": run_id,
            "provenance": {"backend": "docker_workspace", "recipe": "report.v1",
                           "recipe_version": "1.0.0", "note": f"made by {run_id}"}}
    body.update(over)
    return ArtifactOccurrence.parse(body)


# ── the contracts ──────────────────────────────────────────────────────────

def test_a_blob_is_named_by_its_bytes_and_an_occurrence_never_is():
    made = Blob.parse({"sha256": DIGEST, "byte_size": 8, "filename": f"{DIGEST}.csv"})
    assert made.id == f"blob_{DIGEST}"
    assert blob_id_for(DIGEST.upper()) == f"blob_{DIGEST}"

    # Two occurrence ids of the same bytes differ: an occurrence names an event.
    assert new_occurrence_id() != new_occurrence_id()
    with pytest.raises(ContractError) as refused:
        _occurrence("alice", "run-1", id=f"blob_{DIGEST}")
    assert "the identity of its bytes" in str(refused.value)


def test_a_blob_carries_no_owner_and_an_occurrence_carries_everything_else():
    assert not hasattr(Blob.parse(
        {"sha256": DIGEST, "byte_size": 8, "filename": "x.csv"}), "owner")
    made = _occurrence("alice", "run-1", session_id="s1",
                       retention={"policy": "days", "days": 30},
                       relations={"derived_from": ["occ_abc"]},
                       legacy_artifact_id=f"art_{DIGEST[:24]}")
    assert made.owner == "alice" and made.run_id == "run-1"
    assert made.retention.days == 30
    assert made.relations.derived_from == ("occ_abc",)
    assert made.legacy_artifact_id == f"art_{DIGEST[:24]}"


def test_a_blob_filename_is_a_bare_name_and_an_unknown_key_is_an_error():
    with pytest.raises(ContractError) as escaped:
        Blob.parse({"sha256": DIGEST, "byte_size": 8, "filename": "../etc/passwd"})
    assert "bare name" in str(escaped.value)
    with pytest.raises(ContractError) as typo:
        Blob.parse({"sha256": DIGEST, "byte_size": 8, "filename": "x.csv", "onwer": "a"})
    assert "unknown key" in str(typo.value)


def test_a_derivative_id_is_derived_so_regenerating_updates_one_row():
    same = derived_id_for(DIGEST, "thumbnail", "media.thumb", "1.0.0")
    assert derived_id_for(DIGEST, "thumbnail", "media.thumb", "1.0.0") == same
    assert derived_id_for(DIGEST, "thumbnail", "media.thumb", "1.1.0") != same
    made = DerivedArtifact.parse({"source_sha256": DIGEST, "derived_kind": "thumbnail",
                                  "filename": "thumb.png", "recipe": "media.thumb",
                                  "recipe_version": "1.0.0"})
    assert made.id == same
    with pytest.raises(ContractError) as wrong:
        DerivedArtifact.parse({"id": "der_whatever", "source_sha256": DIGEST,
                               "derived_kind": "thumbnail", "filename": "t.png"})
    assert "must be" in str(wrong.value)


def test_an_owner_never_matches_by_being_blank():
    """An unowned occurrence answers False for every named caller. Treating ""
    as a wildcard is how the second producer reads the first one's artifact."""
    unowned = _occurrence("", "run-1")
    assert unowned.belongs_to("alice") is False
    assert unowned.belongs_to("") is False
    assert _occurrence("alice", "run-1").belongs_to("alice") is True


# ── B-017's mandatory matrix ───────────────────────────────────────────────

def test_same_content_two_runs_keeps_both_occurrences(own_database):
    first_blob, created_first = _blob()
    second_blob, created_second = _blob()
    assert (created_first, created_second) == (True, False)
    assert first_blob.id == second_blob.id

    one = ident.record_occurrence(_occurrence("luis", "run-1"))
    two = ident.record_occurrence(_occurrence("luis", "run-2"))
    assert one.id != two.id

    both = ident.occurrences_of(DIGEST)
    assert {o.run_id for o in both} == {"run-1", "run-2"}
    assert {o.provenance.note for o in both} == {"made by run-1", "made by run-2"}
    assert ident.reference_count(DIGEST) == 2
    assert ident.blob(DIGEST).refcount == 2
    # One set of bytes, two artifacts: physical dedupe kept, logical loss gone.
    db = db_mod.SessionLocal()
    try:
        assert db.query(BlobRow).count() == 1
        assert db.query(ArtifactOccurrenceRow).count() == 2
    finally:
        db.close()


def test_same_content_two_owners_do_not_share_the_artifact(own_database, store):
    _blob()
    alice = ident.record_occurrence(_occurrence("alice", "run-a"))
    bob = ident.record_occurrence(_occurrence("bob", "run-b"))

    assert ident.for_owner(alice.id, owner="alice").run_id == "run-a"
    assert ident.for_owner(bob.id, owner="bob").run_id == "run-b"

    # Bob produced the same bytes. That is not a claim on Alice's artifact.
    with pytest.raises(ident.NotTheOwner):
        ident.for_owner(alice.id, owner="bob")
    with pytest.raises(ident.NotTheOwner):
        ident.path_for(alice.id, owner="bob", store_dir=store)

    # And the one thing that must not exist: a way from the hash to the bytes.
    assert not any(name.endswith("_for_hash") for name in dir(ident))
    assert ident.occurrences_of(DIGEST, owner="bob") == (bob,)

    mine = ident.path_for(alice.id, owner="alice", store_dir=store)
    assert os.path.basename(mine) == f"{DIGEST}.csv"
    assert open(mine, "rb").read() == BYTES


def test_deleting_one_occurrence_leaves_the_other_and_its_bytes(own_database, store):
    _blob()
    alice = ident.record_occurrence(_occurrence("alice", "run-a"))
    bob = ident.record_occurrence(_occurrence("bob", "run-b"))

    report = ident.forget_occurrence(alice.id)
    assert report["removed"] is True and report["references_left"] == 1

    with pytest.raises(ident.ArtifactNotFound):
        ident.for_owner(alice.id, owner="alice")
    assert ident.for_owner(bob.id, owner="bob").run_id == "run-b"
    assert ident.blob(DIGEST) is not None
    assert os.path.exists(os.path.join(store, f"{DIGEST}.csv"))

    # A second delete of the same id must not drive the counter below zero: a
    # negative-looking refcount is how a live blob becomes collectable.
    assert ident.forget_occurrence(alice.id)["removed"] is False
    assert ident.blob(DIGEST).refcount == 1


def test_garbage_collection_drops_orphans_and_repairs_drift(own_database, store):
    _blob()
    _blob(digest=OTHER, size=3, ext="bin")
    (open(os.path.join(store, f"{OTHER}.bin"), "wb")).write(b"abc")
    kept = ident.record_occurrence(_occurrence("alice", "run-a"))

    # A refcount that lost an increment is exactly how a collector deletes live
    # bytes, so the collector must not believe it.
    db = db_mod.SessionLocal()
    try:
        db.query(BlobRow).filter(BlobRow.sha256 == DIGEST).update({"refcount": 0})
        db.commit()
    finally:
        db.close()

    report = ident.collect_garbage(store_dir=store, delete_bytes=True)
    assert report["blobs_removed"] == (OTHER,)
    assert report["bytes_unlinked"] == (f"{OTHER}.bin",)
    assert report["refcount_drift"] == ({"sha256": DIGEST, "recorded": 0, "actual": 1},)

    assert not os.path.exists(os.path.join(store, f"{OTHER}.bin"))
    assert os.path.exists(os.path.join(store, f"{DIGEST}.csv"))
    assert ident.blob(DIGEST).refcount == 1
    assert ident.for_owner(kept.id, owner="alice").run_id == "run-a"


def test_the_collector_refuses_bytes_the_old_table_still_names(own_database, store):
    """Until the cutover, `artifacts` and `artifact_blobs` are two censuses of
    the same files and only one of them knows about the other."""
    _blob()
    db = db_mod.SessionLocal()
    try:
        db.add(ArtifactRow(id=f"art_{DIGEST[:24]}", kind="dataset",
                           filename=f"{DIGEST}.csv", sha256=DIGEST, owner="luis"))
        db.commit()
    finally:
        db.close()

    report = ident.collect_garbage(store_dir=store, delete_bytes=True)
    assert report["blobs_removed"] == (DIGEST,)
    assert report["bytes_unlinked"] == ()
    assert report["bytes_kept"] == ({"filename": f"{DIGEST}.csv",
                                     "reason": "the artifacts table still names it"},)
    assert os.path.exists(os.path.join(store, f"{DIGEST}.csv"))


def test_delete_bytes_is_off_by_default(own_database, store):
    _blob()
    report = ident.collect_garbage(store_dir=store)
    assert report["blobs_removed"] == (DIGEST,)
    assert report["bytes_kept"] == ({"filename": f"{DIGEST}.csv",
                                     "reason": "delete_bytes is off"},)
    assert os.path.exists(os.path.join(store, f"{DIGEST}.csv"))


def test_two_writers_of_one_hash_produce_one_blob(own_database):
    """The race `collect()` still has: exists() then move(). Here both writers
    contend for a primary key, so one of them creates and the other reads."""
    start = threading.Barrier(2)
    outcome = {}

    def writer(tag):
        start.wait(timeout=10)
        try:
            made, created = _blob()
            outcome[tag] = (made.id, created, None)
        except Exception as e:                    # noqa: BLE001 - reported, not hidden
            outcome[tag] = (None, None, e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert [outcome[t][2] for t in ("a", "b")] == [None, None], outcome
    assert {outcome[t][0] for t in ("a", "b")} == {f"blob_{DIGEST}"}
    assert sorted(outcome[t][1] for t in ("a", "b")) == [False, True]

    db = db_mod.SessionLocal()
    try:
        assert db.query(BlobRow).count() == 1
    finally:
        db.close()


def test_two_occurrences_recorded_concurrently_both_count(own_database):
    """`refcount = refcount + 1` in SQL, not read-modify-write in Python: two
    simultaneous occurrences of one blob must count as two."""
    _blob()
    start = threading.Barrier(2)
    errors = []

    def writer(run_id):
        start.wait(timeout=10)
        try:
            ident.record_occurrence(_occurrence("luis", run_id))
        except Exception as e:                    # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(r,)) for r in ("run-1", "run-2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert ident.reference_count(DIGEST) == 2
    assert ident.blob(DIGEST).refcount == 2


# ── the refusals ───────────────────────────────────────────────────────────

def test_an_occurrence_without_its_blob_is_refused(own_database):
    with pytest.raises(ident.MissingBlob):
        ident.record_occurrence(_occurrence("luis", "run-1"))


def test_an_occurrence_id_is_never_reused(own_database):
    _blob()
    made = ident.record_occurrence(_occurrence("luis", "run-1"))
    with pytest.raises(ValueError) as reused:
        ident.record_occurrence(_occurrence("someone-else", "run-2", id=made.id))
    assert "never reused" in str(reused.value)
    assert ident.occurrence(made.id).owner == "luis"


def test_a_blob_offered_with_a_different_size_is_refused(own_database):
    _blob()
    with pytest.raises(ValueError) as mismatch:
        _blob(size=999)
    assert "measured a different file" in str(mismatch.value)


def test_a_legacy_id_resolves_only_through_the_stored_alias(own_database):
    """`art_<hash[:24]>` truncated the hash, so the alias cannot be recomputed;
    it is a stored, indexed column or it is nothing."""
    _blob()
    legacy = f"art_{DIGEST[:24]}"
    assert ident.resolve(legacy) is None          # nothing copied yet: the honest answer

    made = ident.record_occurrence(_occurrence("luis", "run-1", legacy_artifact_id=legacy))
    assert ident.resolve(legacy).id == made.id
    assert ident.resolve(made.id).id == made.id
    assert ident.resolve("art_nothing") is None


def test_a_derivative_hangs_off_the_blob_and_is_reached_through_an_occurrence(own_database):
    _blob()
    alice = ident.record_occurrence(_occurrence("alice", "run-a"))
    bob = ident.record_occurrence(_occurrence("bob", "run-b"))
    thumb = DerivedArtifact.parse({"source_sha256": DIGEST, "derived_kind": "thumbnail",
                                   "filename": "t.png", "recipe": "media.thumb",
                                   "recipe_version": "1.0.0", "byte_size": 12})
    ident.record_derivative(thumb)
    ident.record_derivative(thumb)                # regenerating updates one row

    assert [d.id for d in ident.derivatives_for(alice.id, owner="alice")] == [thumb.id]
    # Shared bytes share the thumbnail; they still do not share the artifact.
    assert [d.id for d in ident.derivatives_for(bob.id, owner="bob")] == [thumb.id]
    with pytest.raises(ident.NotTheOwner):
        ident.derivatives_for(alice.id, owner="bob")


def test_a_missing_occurrence_and_a_foreign_one_are_told_apart(own_database, store):
    _blob()
    alice = ident.record_occurrence(_occurrence("alice", "run-a"))
    with pytest.raises(ident.ArtifactNotFound):
        ident.path_for("occ_nothing", owner="alice", store_dir=store)
    with pytest.raises(ident.NotTheOwner):
        ident.path_for(alice.id, owner="bob", store_dir=store)


def test_an_unowned_occurrence_needs_the_caller_to_say_so(own_database, store):
    _blob()
    orphan = ident.record_occurrence(_occurrence("", "run-x"))
    with pytest.raises(ident.NotTheOwner):
        ident.path_for(orphan.id, owner="alice", store_dir=store)
    resolved = ident.path_for(orphan.id, owner="", store_dir=store, allow_unowned=True)
    assert os.path.basename(resolved) == f"{DIGEST}.csv"


def test_an_occurrence_naming_bytes_the_store_lost_says_so(own_database, store):
    _blob()
    made = ident.record_occurrence(_occurrence("luis", "run-1"))
    db = db_mod.SessionLocal()
    try:
        db.query(BlobRow).filter(BlobRow.sha256 == DIGEST).delete()
        db.commit()
    finally:
        db.close()
    with pytest.raises(ident.MissingBlob):
        ident.path_for(made.id, owner="luis", store_dir=store)


# ── the migration is additive and reversible ───────────────────────────────

def test_the_migration_adds_identity_tables_and_edits_none(own_database):
    """Same posture as the artifacts migration: the reverse is a DROP, so it
    cannot depend on which SQLite the user happens to have."""
    columns = {c["name"] for c in inspect(own_database).get_columns("artifacts")}
    assert not any(name.startswith(("blob", "occurrence")) for name in columns)

    db_mod._migrate_create_artifact_identity_tables()      # idempotent on a live schema
    tables = set(inspect(own_database).get_table_names())
    assert {"artifact_blobs", "artifact_occurrences", "artifact_derivatives", "artifact_tombstones"} <= tables

    with own_database.connect() as conn:
        indexes = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"))}
    assert {"ix_occurrences_blob_owner", "ix_derivatives_source_kind"} <= indexes


def test_the_migration_runs_twice_and_reverses(own_database):
    db_mod._migrate_create_artifact_identity_tables()
    db_mod._migrate_create_artifact_identity_tables()
    _blob()
    assert ident.blob(DIGEST) is not None

    db_mod.rollback_artifact_identity_tables()
    tables = set(inspect(own_database).get_table_names())
    assert not ({"artifact_blobs", "artifact_occurrences", "artifact_derivatives", "artifact_tombstones"} & tables)
    assert "artifacts" in tables                  # untouched, and still authoritative


# ── the bug this replaces, kept as a witness ───────────────────────────────

def test_reusing_an_artifact_id_for_another_owner_is_rejected(own_database, tmp_path):
    """A caller retaining the obsolete content-derived id must not silently
    discard Bob's result or give Bob Alice's id. collect() now mints event ids."""
    from src import artifact_store
    from src.contracts import Artifact

    def legacy(owner, run_id):
        return Artifact.parse({"id": f"art_{DIGEST[:24]}", "kind": "dataset",
                               "filename": f"{DIGEST}.csv", "sha256": DIGEST,
                               "byte_size": len(BYTES), "label": "data.csv",
                               "owner": owner, "run_id": run_id,
                               "provenance": {"note": f"made by {run_id}"}})

    assert artifact_store.persist([legacy("alice", "run-a")]) == {"created": 1,
                                                                 "already_there": 0}
    with pytest.raises(ValueError, match='different event'):
        artifact_store.persist([legacy("bob", "run-b")])
    db = db_mod.SessionLocal()
    try:
        rows = db.query(ArtifactOccurrenceRow).all()
        assert len(rows) == 1 and rows[0].owner == "alice"
        assert "run-b" not in (rows[0].provenance_note or "")
    finally:
        db.close()

    ident.record_occurrence(_occurrence("alice", "run-a"))
    ident.record_occurrence(_occurrence("bob", "run-b"))
    assert {o.owner for o in ident.occurrences_of(DIGEST)} == {"alice", "bob"}
