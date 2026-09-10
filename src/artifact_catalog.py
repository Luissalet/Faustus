"""Read projection shared by project context and the state mirror.

Occurrences are authoritative. Historical rows remain readable until their
insert-only copy succeeds, without writes or file hashing on a read request.
The caller must scope/authorize before reading file contents.

ART-08 (Biblioteca universal de resultados): ``search()`` is the same
current+historical union ``recent()`` already does, narrowed by the facets the
Library UI needs — type, date range, conversation (session) and a text term —
so every artifact kind indexed here (whatever a run collected via
``artifact_store.collect()``) is findable the same way, instead of a second
parallel index per artifact type. ``recent()`` keeps its original signature
and behaviour; the filters are additive keyword-only arguments so the one
existing caller (routes/artifact_routes.py) is unaffected until it opts in.
"""
from datetime import datetime, timezone
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


def _parse_date_bound(value, *, label):
    """Accept an ISO-8601 date/datetime string; ``None``/empty means unbounded.

    Raises ``ValueError`` on garbage input rather than silently dropping the
    filter — a typo in a date should not quietly return the unfiltered set.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 date/datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recent(db, *, owner='', project_id='', session_id='', kind='', q='',
          since='', until='', limit=200):
    """Every artifact this owner can see, current occurrences plus historical
    rows not yet migrated, newest first.

    ``session_id``/``kind``/``q`` (substring, case-insensitive, matched
    against the label) and ``since``/``until`` (inclusive ISO-8601 bounds) are
    additive facets for the Library search (ART-08); omitting all of them
    reproduces the exact result the original two-argument callers got.
    """
    from core.database import ArtifactRow as Old, ArtifactOccurrenceRow as Occ, BlobRow, ArtifactTombstoneRow as Gone
    limit = max(1, min(int(limit), 200))
    since_dt = _parse_date_bound(since, label='since')
    until_dt = _parse_date_bound(until, label='until')
    current = db.query(Occ, BlobRow).join(BlobRow, BlobRow.sha256 == Occ.blob_sha256)
    old = db.query(Old).filter(~Old.id.in_(
        db.query(Occ.legacy_artifact_id).filter(Occ.legacy_artifact_id.isnot(None))),
        ~Old.id.in_(db.query(Gone.legacy_artifact_id).filter(Gone.legacy_artifact_id.isnot(None))))
    for name, value in (('owner', owner), ('project_id', project_id),
                        ('session_id', session_id), ('kind', kind)):
        if value:
            current = current.filter(getattr(Occ, name) == value)
            old = old.filter(getattr(Old, name) == value)
    if q:
        term = f'%{q}%'
        current = current.filter(Occ.label.ilike(term))
        old = old.filter(Old.label.ilike(term))
    if since_dt is not None:
        current = current.filter(Occ.created_at_iso >= since_dt.isoformat())
        old = old.filter(Old.created_at >= since_dt.replace(tzinfo=None))
    if until_dt is not None:
        current = current.filter(Occ.created_at_iso <= until_dt.isoformat())
        old = old.filter(Old.created_at <= until_dt.replace(tzinfo=None))
    rows = [_project(*pair) for pair in current.order_by(
        Occ.created_at_iso.desc(), Occ.id.desc()).limit(limit).all()]
    rows.extend(old.order_by(Old.created_at.desc(), Old.id.desc()).limit(limit).all())
    return sorted(rows, key=lambda row: (str(row.created_at), row.id), reverse=True)[:limit]


def path(row, *, store_dir=None):
    from src.artifact_store import path_of
    from src.constants import GENERATED_IMAGES_DIR
    return path_of(row.filename, store_dir=(GENERATED_IMAGES_DIR
                   if getattr(row, 'legacy_gallery_id', None) else store_dir))
