"""
artifact_identity.py — blobs, occurrences and derivatives (B-017, lot ART-1).

The production collector gives each event its own identity while this module
deduplicates its bytes. It writes only the three tables —
`artifact_blobs`, `artifact_occurrences`, `artifact_derivatives` — and reads
`artifacts` for exactly one purpose: refusing to delete bytes the old table
still names.

`collect()` and `persist()` use this store. The insert-only background copy in
`src/artifact_migration.py` preserves historical ids as explicit aliases.

Four rules, each of which has a tempting wrong version:

* **A blob is created by winning a primary key, not by passing a check.**
  `os.path.exists()` then `shutil.move()` is two writers away from one of them
  losing its row; `INSERT` then catch `IntegrityError` has one winner by
  construction, and the loser reads what the winner wrote.
* **Physical deduplication grants no logical access.** Every read path takes an
  occurrence id and an owner. There is no function here that turns a hash into
  a path, because that function is the bug written as an API.
* **The refcount is an index, the occurrence count is the truth.** A stored
  counter can drift; `collect_garbage()` counts rows, repairs the counter and
  reports the drift rather than trusting it and deleting live bytes.
* **The collector refuses bytes the old table still names.** Until `artifacts`
  stops being a census of references, two censuses cover the same files, and
  only one of them knows about the other.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.artifact import Provenance, Retention
from src.contracts.base import now_iso
from src.contracts.blob import (
    ArtifactOccurrence, Blob, DerivedArtifact, Relations, blob_id_for,
    derived_id_for, new_occurrence_id,
)

logger = logging.getLogger(__name__)

#: SQLite serialises writers; two threads recording occurrences of the same
#: blob can meet a held write lock. Short, bounded, and never silent: the last
#: failure is raised rather than swallowed into a wrong answer.
_LOCK_RETRIES = 5
_LOCK_BACKOFF = 0.05


class ArtifactNotFound(LookupError):
    """No occurrence with that id, and no legacy alias pointing at one."""


class NotTheOwner(PermissionError):
    """The occurrence exists and belongs to somebody else.

    Distinct from ArtifactNotFound on purpose: the caller asked for something
    real. Callers that must not leak existence should map both to one answer at
    their own boundary, where they know who is asking."""


class MissingBlob(ValueError):
    """An occurrence whose bytes are not in `artifact_blobs`.

    Only reachable if a writer inserted an occurrence without its blob, which
    `record_occurrence()` refuses to do — so this is a corrupted store, not a
    routine outcome, and it says so instead of returning None."""


# ── row ↔ contract ─────────────────────────────────────────────────────────

def _to_blob(row) -> Blob:
    return Blob(sha256=row.sha256, byte_size=row.byte_size, filename=row.filename,
                media_type=row.media_type or "", refcount=row.refcount or 0,
                created_at=row.created_at_iso or "")


def _json_list(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("unreadable JSON list in an occurrence row; treating it as empty")
        return ()
    return tuple(str(x) for x in loaded) if isinstance(loaded, list) else ()


def _to_occurrence(row) -> ArtifactOccurrence:
    relations = Relations()
    if row.relations_json:
        try:
            relations = Relations.parse(json.loads(row.relations_json), "occurrence.relations")
        except Exception:
            logger.warning("unreadable relations on occurrence %s; reporting none", row.id)
    return ArtifactOccurrence(
        id=row.id, kind=row.kind, blob_sha256=row.blob_sha256,
        label=row.label or "", owner=row.owner or "", project_id=row.project_id or "",
        run_id=row.run_id or "", session_id=row.session_id or "",
        skill_id=row.skill_id or "", skill_version=row.skill_version or "",
        created_at=row.created_at_iso or "", partial=bool(row.partial),
        approval_id=row.approval_id or "",
        provenance=Provenance(
            model=row.model, model_license=row.model_license, backend=row.backend,
            recipe=row.recipe, recipe_version=row.recipe_version,
            recipe_fingerprint=row.recipe_fingerprint, inputs_digest=row.inputs_digest,
            seed=row.seed, engine=row.engine, engine_job_id=row.engine_job_id,
            source_artifact_ids=_json_list(row.source_artifact_ids),
            note=row.provenance_note or ""),
        retention=Retention(policy=row.retention_policy or "keep",
                            days=row.retention_days,
                            reason=row.retention_reason or ""),
        relations=relations,
        legacy_artifact_id=row.legacy_artifact_id or "",
    )


def _to_derived(row) -> DerivedArtifact:
    return DerivedArtifact(
        id=row.id, source_sha256=row.source_sha256, derived_kind=row.derived_kind,
        filename=row.filename, sha256=row.sha256 or "", byte_size=row.byte_size,
        media_type=row.media_type or "", recipe=row.recipe or "",
        recipe_version=row.recipe_version or "", created_at=row.created_at_iso or "")


def _retry_on_lock(action, what: str):
    """SQLite's writer lock is held for microseconds here; a contended write is
    worth waiting for and a genuinely broken one is worth raising."""
    from sqlalchemy.exc import OperationalError

    for attempt in range(_LOCK_RETRIES):
        try:
            return action()
        except OperationalError as e:
            if "locked" not in str(e).lower() or attempt == _LOCK_RETRIES - 1:
                raise
            logger.debug("%s: database busy, retry %d", what, attempt + 1)
            time.sleep(_LOCK_BACKOFF * (attempt + 1))
    raise RuntimeError(f"{what}: unreachable")


# ── blobs ──────────────────────────────────────────────────────────────────

def ensure_blob(*, sha256: str, byte_size: int, filename: str,
                media_type: str = "") -> Tuple[Blob, bool]:
    """Get the blob for these bytes, creating it if this caller wins the race.

    Returns `(blob, created)`. Two writers of the same content both get the
    same blob and exactly one of them gets `created=True`, because they race on
    the primary key `blob_<sha256>` rather than on a prior existence check —
    which is the failure mode `collect()` still has today.

    A second recording of the same hash with a different size is refused rather
    than reconciled: with SHA-256 that means one of the two callers measured
    the wrong file, and picking either number silently would make the store
    lie about what is on disk."""
    from sqlalchemy.exc import IntegrityError

    from core.database import BlobRow, SessionLocal

    blob = Blob.parse({"sha256": sha256, "byte_size": byte_size,
                       "filename": filename, "media_type": media_type,
                       "created_at": now_iso()})

    def attempt() -> Tuple[Blob, bool]:
        db = SessionLocal()
        try:
            row = db.get(BlobRow, blob.id)
            if row is None:
                db.add(BlobRow(id=blob.id, sha256=blob.sha256,
                               byte_size=blob.byte_size, media_type=blob.media_type or None,
                               filename=blob.filename, refcount=0,
                               created_at_iso=blob.created_at, schema_version=1))
                try:
                    db.commit()
                except IntegrityError:
                    # The other writer got there between our get() and our
                    # commit(). Its row is as good as ours would have been.
                    db.rollback()
                    row = db.get(BlobRow, blob.id)
                    if row is None:
                        raise
                else:
                    return blob, True
            existing = _to_blob(row)
            if existing.byte_size != blob.byte_size:
                raise ValueError(
                    f"blob {blob.sha256} is recorded as {existing.byte_size} bytes "
                    f"and was offered as {blob.byte_size}; one of the two callers "
                    f"measured a different file than it hashed")
            return existing, False
        finally:
            db.close()

    return _retry_on_lock(attempt, "ensure_blob")


def blob(sha256: str) -> Optional[Blob]:
    from core.database import BlobRow, SessionLocal

    db = SessionLocal()
    try:
        row = db.get(BlobRow, blob_id_for(sha256))
        return _to_blob(row) if row is not None else None
    finally:
        db.close()


def reference_count(sha256: str) -> int:
    """How many occurrences actually point at these bytes.

    This is the authority. `Blob.refcount` is a cached copy of it kept for
    cheap listing, and `collect_garbage()` treats any difference as drift to
    repair and report, never as a reason to delete."""
    from core.database import ArtifactOccurrenceRow, SessionLocal

    db = SessionLocal()
    try:
        return int(db.query(ArtifactOccurrenceRow)
                   .filter(ArtifactOccurrenceRow.blob_sha256 == (sha256 or "").lower())
                   .count())
    finally:
        db.close()


# ── occurrences ────────────────────────────────────────────────────────────

def record_occurrence(occurrence: ArtifactOccurrence) -> ArtifactOccurrence:
    """Insert one logical artifact and claim a reference on its blob.

    The insert and the refcount bump are one transaction: an occurrence whose
    blob was never counted is a blob a collector will delete out from under it.
    The bump is `refcount = refcount + 1` in SQL rather than read-modify-write
    in Python, so two simultaneous occurrences of one blob count as two.

    Refuses an occurrence whose blob is not recorded — `ensure_blob()` first,
    always — and refuses to reuse an id, because an id that can be overwritten
    is an id that can lose a provenance, which is the whole bug."""
    from core.database import ArtifactOccurrenceRow, BlobRow, SessionLocal, ArtifactTombstoneRow as Gone

    def attempt() -> ArtifactOccurrence:
        db = SessionLocal()
        try:
            if db.get(BlobRow, blob_id_for(occurrence.blob_sha256)) is None:
                raise MissingBlob(
                    f"blob {occurrence.blob_sha256} is not recorded; call "
                    f"ensure_blob() before recording an occurrence of it")
            if db.get(ArtifactOccurrenceRow, occurrence.id) is not None:
                raise ValueError(
                    f"occurrence {occurrence.id} already exists; occurrence ids name "
                    f"an event and are never reused")
            p = occurrence.provenance
            if db.get(Gone, occurrence.id) is not None or (
                    occurrence.legacy_artifact_id and db.query(Gone).filter(
                        Gone.legacy_artifact_id == occurrence.legacy_artifact_id).first()):
                raise ValueError('this artifact was explicitly removed and cannot be replayed')
            db.add(ArtifactOccurrenceRow(
                id=occurrence.id, blob_sha256=occurrence.blob_sha256,
                kind=occurrence.kind, label=occurrence.label, partial=occurrence.partial,
                owner=occurrence.owner or None, project_id=occurrence.project_id or None,
                run_id=occurrence.run_id or None, session_id=occurrence.session_id or None,
                skill_id=occurrence.skill_id or None,
                skill_version=occurrence.skill_version or None,
                approval_id=occurrence.approval_id or None,
                model=p.model, model_license=p.model_license, backend=p.backend,
                recipe=p.recipe, recipe_version=p.recipe_version,
                recipe_fingerprint=p.recipe_fingerprint, inputs_digest=p.inputs_digest,
                seed=p.seed, engine=p.engine, engine_job_id=p.engine_job_id,
                source_artifact_ids=(json.dumps(list(p.source_artifact_ids))
                                     if p.source_artifact_ids else None),
                provenance_note=p.note or "",
                retention_policy=occurrence.retention.policy,
                retention_days=occurrence.retention.days,
                retention_reason=occurrence.retention.reason or "",
                relations_json=(json.dumps(occurrence.relations.to_dict())
                                if not occurrence.relations.is_empty() else None),
                legacy_artifact_id=occurrence.legacy_artifact_id or None,
                created_at_iso=occurrence.created_at or now_iso(),
                schema_version=occurrence.schema_version,
            ))
            db.query(BlobRow).filter(BlobRow.sha256 == occurrence.blob_sha256).update(
                {BlobRow.refcount: BlobRow.refcount + 1}, synchronize_session=False)
            db.commit()
            return occurrence
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return _retry_on_lock(attempt, "record_occurrence")


def occurrence(occurrence_id: str) -> Optional[ArtifactOccurrence]:
    """One occurrence by its own id. No owner check — the caller that needs one
    uses `path_for()` or `for_owner()`; this is the raw read the migration and
    the admin tools need."""
    from core.database import ArtifactOccurrenceRow, SessionLocal

    db = SessionLocal()
    try:
        row = db.get(ArtifactOccurrenceRow, occurrence_id)
        return _to_occurrence(row) if row is not None else None
    finally:
        db.close()


def ensure_occurrence(value: ArtifactOccurrence) -> Tuple[ArtifactOccurrence, bool]:
    """Idempotent delivery of ONE event, never deduplication by its bytes.

    A retry can differ in its collection timestamp, but cannot change owner,
    provenance, retention or any other logical field under an existing id.
    Concurrent inserts race on the primary key and increment refcount once.
    """
    from sqlalchemy.exc import IntegrityError

    def equivalent(found):
        return ({k: v for k, v in found.to_dict().items() if k != 'created_at'} ==
                {k: v for k, v in value.to_dict().items() if k != 'created_at'})

    found = occurrence(value.id)
    if found is not None:
        if not equivalent(found):
            raise ValueError('artifact occurrence id already belongs to a different event')
        return found, False
    try:
        return record_occurrence(value), True
    except (IntegrityError, ValueError):
        found = occurrence(value.id)
        if found is None or not equivalent(found):
            raise
        return found, False


def resolve(artifact_id: str) -> Optional[ArtifactOccurrence]:
    """Accept either an occurrence id or a legacy `art_<hash>` id.

    The alias is a stored, indexed column and not a string transformation
    because the old id truncated the hash to 24 characters and cannot be
    inverted. Before the copy phase runs there is nothing to alias and this
    returns None for every legacy id — which is the honest answer, not a
    fallback into the old table dressed up as an occurrence."""
    from core.database import ArtifactOccurrenceRow, SessionLocal

    if not artifact_id:
        return None
    db = SessionLocal()
    try:
        row = db.get(ArtifactOccurrenceRow, artifact_id)
        if row is None:
            row = (db.query(ArtifactOccurrenceRow)
                   .filter(ArtifactOccurrenceRow.legacy_artifact_id == artifact_id)
                   .one_or_none())
        return _to_occurrence(row) if row is not None else None
    finally:
        db.close()


def occurrences_of(sha256: str, *, owner: Optional[str] = None
                   ) -> Tuple[ArtifactOccurrence, ...]:
    """Every artifact made of these bytes, optionally only this owner's.

    The list is what `artifacts` cannot express: one row per content means one
    owner per content. Passing `owner` filters rather than authorises — it is
    the query behind "my artifacts", not an access check."""
    from core.database import ArtifactOccurrenceRow, SessionLocal

    db = SessionLocal()
    try:
        query = (db.query(ArtifactOccurrenceRow)
                 .filter(ArtifactOccurrenceRow.blob_sha256 == (sha256 or "").lower()))
        if owner is not None:
            query = query.filter(ArtifactOccurrenceRow.owner == (owner or None))
        return tuple(_to_occurrence(r) for r in
                     query.order_by(ArtifactOccurrenceRow.created_at_iso,
                                    ArtifactOccurrenceRow.id).all())
    finally:
        db.close()


def for_owner(occurrence_id: str, *, owner: str) -> ArtifactOccurrence:
    """The occurrence, or a refusal naming which of the two reasons applies.

    This is the only door: sharing a blob is not sharing an artifact, so a
    caller that has the bytes' hash still cannot get here without an occurrence
    id that is theirs."""
    found = resolve(occurrence_id)
    if found is None:
        raise ArtifactNotFound(f"no artifact occurrence {occurrence_id!r}")
    if not found.belongs_to(owner):
        raise NotTheOwner(
            f"occurrence {found.id} belongs to {found.owner or '<nobody>'}, not to "
            f"{owner or '<nobody>'}; identical bytes are not shared ownership")
    return found


def path_for(occurrence_id: str, *, owner: str, store_dir: Optional[str] = None,
             allow_unowned: bool = False) -> str:
    """Resolve an occurrence to the file its bytes live in.

    There is deliberately no `path_for_hash()`. Turning content into a path is
    exactly how physical deduplication turns into logical access, and the point
    of this module is that it cannot.

    `allow_unowned` is for the one legitimate case: an occurrence that records
    no owner at all, such as anything the deferred copy imports from a gallery
    row that never had one. The caller has to say out loud that it is reaching
    for something nobody owns, because the alternative — treating an empty
    owner as a wildcard — hands every unowned artifact to every caller."""
    from src import artifact_store

    found = resolve(occurrence_id)
    if found is None:
        raise ArtifactNotFound(f"no artifact occurrence {occurrence_id!r}")
    if not (found.belongs_to(owner) or (allow_unowned and not found.owner)):
        raise NotTheOwner(
            f"occurrence {found.id} belongs to {found.owner or '<nobody>'}, not to "
            f"{owner or '<nobody>'}; identical bytes are not shared ownership")
    bytes_row = blob(found.blob_sha256)
    if bytes_row is None:
        raise MissingBlob(
            f"occurrence {found.id} names blob {found.blob_sha256}, which is not "
            f"recorded; the store is inconsistent, not empty")
    return artifact_store.path_of(bytes_row.filename, store_dir=store_dir)


def forget_occurrence(occurrence_id: str) -> Dict[str, Any]:
    """Delete one occurrence and release its reference. The blob survives while
    any other occurrence still points at it, which is the property `artifacts`
    could not have: there, one row per content meant deleting the content.

    Returns what is left, so the caller can see that the sibling survived
    rather than having to ask again."""
    from core.database import ArtifactOccurrenceRow, BlobRow, SessionLocal, ArtifactTombstoneRow

    def attempt() -> Dict[str, Any]:
        db = SessionLocal()
        try:
            row = db.get(ArtifactOccurrenceRow, occurrence_id)
            if row is None:
                return {"removed": False, "reason": "no such occurrence",
                        "blob_sha256": "", "references_left": 0}
            digest = row.blob_sha256
            removed = db.query(ArtifactOccurrenceRow).filter(
                ArtifactOccurrenceRow.id == occurrence_id).delete(synchronize_session=False)
            if not removed:
                db.rollback()
                return {'removed': False, 'reason': 'already removed',
                        'blob_sha256': digest, 'references_left': reference_count(digest)}
            db.add(ArtifactTombstoneRow(id=occurrence_id,
                                       legacy_artifact_id=row.legacy_artifact_id or None))
            # Guarded so a double delete cannot drive the counter negative and
            # make a live blob look collectable.
            db.query(BlobRow).filter(BlobRow.sha256 == digest,
                                     BlobRow.refcount > 0).update(
                {BlobRow.refcount: BlobRow.refcount - 1}, synchronize_session=False)
            db.commit()
            left = int(db.query(ArtifactOccurrenceRow)
                       .filter(ArtifactOccurrenceRow.blob_sha256 == digest).count())
            return {"removed": True, "reason": "", "blob_sha256": digest,
                    "references_left": left}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return _retry_on_lock(attempt, "forget_occurrence")


# ── derivatives ────────────────────────────────────────────────────────────

def record_derivative(derived: DerivedArtifact) -> DerivedArtifact:
    """Store or replace a regenerable rendering of a blob.

    Idempotent by construction: the id is derived from source, kind, recipe and
    version, so a factory that runs twice updates one row. Requires the source
    blob to exist, for the same reason an occurrence does."""
    from core.database import BlobRow, DerivedArtifactRow, SessionLocal

    def attempt() -> DerivedArtifact:
        db = SessionLocal()
        try:
            if db.get(BlobRow, blob_id_for(derived.source_sha256)) is None:
                raise MissingBlob(
                    f"blob {derived.source_sha256} is not recorded; a derivative of "
                    f"bytes the store does not have cannot be regenerated")
            row = db.get(DerivedArtifactRow, derived.id)
            if row is None:
                row = DerivedArtifactRow(id=derived.id, source_sha256=derived.source_sha256,
                                         derived_kind=derived.derived_kind,
                                         created_at_iso=derived.created_at or now_iso(),
                                         schema_version=derived.schema_version)
                db.add(row)
            row.filename = derived.filename
            row.sha256 = derived.sha256 or None
            row.byte_size = derived.byte_size
            row.media_type = derived.media_type or None
            row.recipe = derived.recipe or None
            row.recipe_version = derived.recipe_version or None
            db.commit()
            return derived
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    from sqlalchemy.exc import IntegrityError
    try:
        return _retry_on_lock(attempt, "record_derivative")
    except IntegrityError:
        # Another publisher can insert the deterministic id between SELECT
        # and INSERT. Retry as an update; unrelated constraint errors still
        # propagate on the second attempt.
        return _retry_on_lock(attempt, "record_derivative")


def derivatives_for(occurrence_id: str, *, owner: str,
                    allow_unowned: bool = False) -> Tuple[DerivedArtifact, ...]:
    """The derivatives of an occurrence's bytes, reached through the occurrence.

    Derivatives hang off the blob so that two owners of one original share one
    thumbnail instead of storing it twice; they are reachable only this way so
    that sharing the thumbnail does not share the artifact."""
    from core.database import DerivedArtifactRow, SessionLocal

    found = resolve(occurrence_id)
    if found is None:
        raise ArtifactNotFound(f"no artifact occurrence {occurrence_id!r}")
    if not (found.belongs_to(owner) or (allow_unowned and not found.owner)):
        raise NotTheOwner(
            f"occurrence {found.id} belongs to {found.owner or '<nobody>'}, not to "
            f"{owner or '<nobody>'}")
    db = SessionLocal()
    try:
        rows = (db.query(DerivedArtifactRow)
                .filter(DerivedArtifactRow.source_sha256 == found.blob_sha256)
                .order_by(DerivedArtifactRow.derived_kind, DerivedArtifactRow.id).all())
        return tuple(_to_derived(r) for r in rows)
    finally:
        db.close()


# ── garbage collection ─────────────────────────────────────────────────────

def _legacy_references() -> Tuple[set, set]:
    """What the pre-split `artifacts` table still names, by hash and by name.

    Read, never written. While that table remains the census the rest of the
    app reads, its rows are references this collector must honour even though
    nothing in the new tables counts them."""
    from core.database import ArtifactRow, SessionLocal

    db = SessionLocal()
    try:
        hashes = {h for (h,) in db.query(ArtifactRow.sha256).all() if h}
        names = {n for (n,) in db.query(ArtifactRow.filename).all() if n}
        return hashes, names
    except Exception:
        # A store whose legacy table cannot be read must not become a store
        # that deletes freely: claim everything is referenced.
        logger.warning("could not read the legacy artifacts table; treating every "
                       "blob as still referenced", exc_info=True)
        return {"*"}, {"*"}
    finally:
        db.close()


def collect_garbage(*, store_dir: Optional[str] = None,
                    delete_bytes: bool = False) -> Dict[str, Any]:
    """Drop blob rows nothing points at, and say what was left on disk.

    Two deliberate refusals:

    * It counts occurrences instead of trusting `refcount`, repairs the stored
      counter and reports the drift. A counter that lost an increment is how a
      collector deletes bytes somebody is still using.
    * `delete_bytes` is off by default and, even when on, refuses any file the
      `artifacts` table still names by hash or by filename. Until the cutover
      in `docs/design/ART-1-artifacts.md` lands, two censuses cover the same
      files and only this one knows it.

    Removing the row while leaving the file is not a leak in this phase: the
    old table is still authoritative for what exists on disk, and every file
    left behind is named in the return value."""
    from core.database import (ArtifactOccurrenceRow, BlobRow, DerivedArtifactRow,
                               SessionLocal)

    legacy_hashes, legacy_names = _legacy_references()
    removed: List[str] = []
    unlinked: List[str] = []
    kept: List[Dict[str, str]] = []
    drift: List[Dict[str, Any]] = []
    derivatives_removed = 0

    db = SessionLocal()
    try:
        for row in db.query(BlobRow).all():
            actual = int(db.query(ArtifactOccurrenceRow)
                         .filter(ArtifactOccurrenceRow.blob_sha256 == row.sha256).count())
            if (row.refcount or 0) != actual:
                drift.append({"sha256": row.sha256, "recorded": row.refcount or 0,
                              "actual": actual})
                row.refcount = actual
            if actual:
                continue
            filename = row.filename
            digest = row.sha256
            derivatives = (db.query(DerivedArtifactRow)
                           .filter(DerivedArtifactRow.source_sha256 == digest).all())
            for derivative in derivatives:
                db.delete(derivative)
                derivatives_removed += 1
            db.delete(row)
            removed.append(digest)
            if not delete_bytes:
                kept.append({"filename": filename, "reason": "delete_bytes is off"})
            elif digest in legacy_hashes or filename in legacy_names:
                kept.append({"filename": filename,
                             "reason": "the artifacts table still names it"})
            else:
                unlinked.append(filename)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    from src import artifact_store

    for filename in list(unlinked):
        try:
            os.unlink(artifact_store.path_of(filename, store_dir=store_dir))
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove collected blob %s", filename, exc_info=True)
            unlinked.remove(filename)
            kept.append({"filename": filename, "reason": "the unlink failed"})

    if removed or drift:
        logger.info("artifact gc: %d blob rows removed, %d files unlinked, "
                    "%d refcounts repaired", len(removed), len(unlinked), len(drift))
    return {"blobs_removed": tuple(removed), "bytes_unlinked": tuple(unlinked),
            "bytes_kept": tuple(kept), "refcount_drift": tuple(drift),
            "derivatives_removed": derivatives_removed}


__all__ = [
    "ArtifactNotFound", "NotTheOwner", "MissingBlob",
    "ensure_blob", "blob", "reference_count",
    "record_occurrence", "occurrence", "resolve", "occurrences_of", "for_owner",
    "path_for", "forget_occurrence",
    "record_derivative", "derivatives_for", "collect_garbage",
    "new_occurrence_id", "blob_id_for", "derived_id_for",
]
