"""Read projection shared by project context and the state mirror.

Occurrences are authoritative. Historical rows remain readable until their
insert-only copy succeeds, without writes or file hashing on a read request.
The caller must scope/authorize before reading file contents.
"""
from types import SimpleNamespace


def _project(row, blob):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    values.update(filename=blob.filename, sha256=blob.sha256,
                  byte_size=blob.byte_size, media_type=blob.media_type,
                  created_at=row.created_at_iso, legacy_gallery_id=None)
    return SimpleNamespace(**values)


def get(db, artifact_id):
    from sqlalchemy import or_
    from core.database import ArtifactRow, ArtifactOccurrenceRow as Occ, BlobRow, ArtifactTombstoneRow as Gone
    if db.query(Gone).filter(or_(Gone.id == artifact_id, Gone.legacy_artifact_id == artifact_id)).first():
        return None
    pair = (db.query(Occ, BlobRow).join(BlobRow, BlobRow.sha256 == Occ.blob_sha256)
            .filter(or_(Occ.id == artifact_id, Occ.legacy_artifact_id == artifact_id))
            .first())
    if pair:
        return _project(*pair)
    return db.get(ArtifactRow, artifact_id)


def recent(db, *, owner='', project_id='', limit=200):
    from core.database import ArtifactRow as Old, ArtifactOccurrenceRow as Occ, BlobRow, ArtifactTombstoneRow as Gone
    limit = max(1, min(int(limit), 200))
    current = db.query(Occ, BlobRow).join(BlobRow, BlobRow.sha256 == Occ.blob_sha256)
    old = db.query(Old).filter(~Old.id.in_(
        db.query(Occ.legacy_artifact_id).filter(Occ.legacy_artifact_id.isnot(None))),
        ~Old.id.in_(db.query(Gone.legacy_artifact_id).filter(Gone.legacy_artifact_id.isnot(None))))
    for name, value in (('owner', owner), ('project_id', project_id)):
        if value:
            current = current.filter(getattr(Occ, name) == value)
            old = old.filter(getattr(Old, name) == value)
    rows = [_project(*pair) for pair in current.order_by(
        Occ.created_at_iso.desc(), Occ.id.desc()).limit(limit).all()]
    rows.extend(old.order_by(Old.created_at.desc(), Old.id.desc()).limit(limit).all())
    return sorted(rows, key=lambda row: (str(row.created_at), row.id), reverse=True)[:limit]


def path(row, *, store_dir=None):
    from src.artifact_store import path_of
    from src.constants import GENERATED_IMAGES_DIR
    return path_of(row.filename, store_dir=(GENERATED_IMAGES_DIR
                   if getattr(row, 'legacy_gallery_id', None) else store_dir))
