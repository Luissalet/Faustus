"""Insert-only legacy artifact copy. Existing rows and gallery files survive.

Legacy ids become stored aliases, not reconstructed hashes. Hashless gallery
rows are measured from a confined file; a missing file is reported, never
represented by a fabricated blob. Repeating the copy preserves refcounts.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import uuid
from datetime import timezone

from src.contracts.blob import ArtifactOccurrence, DerivedArtifact
from src.contracts.artifact import Provenance
from src import artifact_identity as identity

logger = logging.getLogger(__name__)


def copy_legacy(*, store_dir=None, gallery_dir=None, only_id=None, after_id='', limit=50):
    from core.database import SessionLocal, ArtifactRow, ArtifactOccurrenceRow, ArtifactTombstoneRow as Gone
    from src.constants import GENERATED_IMAGES_DIR
    from src.artifact_store import ARTIFACT_STORE_DIR, path_of, publish_copy, sha256_of
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('migration batch limit must be between 1 and 200')
    destination = store_dir or ARTIFACT_STORE_DIR
    gallery = gallery_dir or GENERATED_IMAGES_DIR
    report = {'created': 0, 'skipped': [], 'already_there': 0, 'next_cursor': None}
    with SessionLocal() as db:
        query = db.query(ArtifactRow).filter(~ArtifactRow.id.in_(
            db.query(ArtifactOccurrenceRow.legacy_artifact_id).filter(
                ArtifactOccurrenceRow.legacy_artifact_id.isnot(None))),
            ~ArtifactRow.id.in_(db.query(Gone.legacy_artifact_id).filter(Gone.legacy_artifact_id.isnot(None))))
        if only_id:
            query = query.filter(ArtifactRow.id == only_id)
        if after_id:
            query = query.filter(ArtifactRow.id > after_id)
        rows = query.order_by(ArtifactRow.id).limit(limit).all()
        if len(rows) == limit:
            report['next_cursor'] = rows[-1].id
        for row in rows:
            try:
                filename = row.filename
                source = path_of(filename, store_dir=gallery if row.legacy_gallery_id else destination)
                if not os.path.isfile(source):
                    raise ValueError('legacy artifact file is missing')
                measured = sha256_of(source)
                if row.sha256 and row.sha256.lower() != measured:
                    raise ValueError('legacy artifact hash does not match its bytes')
                size = os.path.getsize(source)
                if row.legacy_gallery_id:
                    measured, size, filename, _ = publish_copy(source, destination, filename)
                    if row.sha256 and row.sha256.lower() != measured:
                        raise ValueError('legacy gallery file changed while being copied')
                identity.ensure_blob(sha256=measured, byte_size=size, filename=filename,
                                     media_type=row.media_type or mimetypes.guess_type(filename)[0] or '')
                provenance = {name: getattr(row, name, None) for name in Provenance._KEYS
                              if name not in ('source_artifact_ids', 'note')}
                provenance['note'] = row.provenance_note or ''
                created = row.created_at
                created_iso = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None
                               else created.astimezone(timezone.utc)).isoformat().replace('+00:00', 'Z')
                value = ArtifactOccurrence.parse({
                    'id': 'occ_' + uuid.uuid5(uuid.NAMESPACE_URL, 'faustus-legacy:' + row.id).hex,
                    'legacy_artifact_id': row.id, 'blob_sha256': measured, 'kind': row.kind,
                    'label': row.label or '', 'owner': row.owner or '',
                    'project_id': row.project_id or '', 'run_id': row.run_id or '',
                    'session_id': row.session_id or '', 'skill_id': row.skill_id or '',
                    'skill_version': row.skill_version or '', 'created_at': created_iso,
                    'partial': bool(row.partial), 'provenance': provenance,
                    'retention': {'policy': row.retention_policy or 'keep',
                                  'days': row.retention_days, 'reason': row.retention_reason or ''},
                })
                # Preview has no owner of its own; access stays through the
                # occurrence. Copying does not remove or rename the old file.
                if row.preview_filename:
                    preview_source = path_of(row.preview_filename, store_dir=destination)
                    preview_hash = sha256_of(preview_source)
                    identity.record_derivative(DerivedArtifact.parse({
                        'source_sha256': measured, 'derived_kind': 'preview',
                        'filename': row.preview_filename, 'sha256': preview_hash,
                        'byte_size': os.path.getsize(preview_source),
                    }))
                _, made = identity.ensure_occurrence(value)
                report['created' if made else 'already_there'] += 1
            except (ValueError, OSError) as exc:
                report['skipped'].append({'id': row.id, 'reason': str(exc)})
    if report['skipped']:
        logger.warning('Legacy artifact copy needs attention: %s', report['skipped'])
    return report


def migrate_manifests(*, dry_run: bool = True, limit: int = 200, after_id: str = ''
                      ) -> dict:
    """Backfill a version-1 manifest (`state=generated`) for every occurrence
    that does not have one yet — including the ones `copy_legacy()` already
    created, since that copy predates the manifest table.

    `dry_run=True` by default, matching the batch's rule: nothing is written,
    only counted, until a caller explicitly asks for the real pass. Idempotent
    either way — an occurrence with a manifest already is skipped, so running
    this repeatedly (or resuming with `after_id`) never appends a second
    version-1. Deliberately does not re-validate the file: a bulk pass over a
    large store opening every DOCX/XLSX/PDF would be exactly the intensive
    load the batch's rules ask this lot to avoid. A backfilled manifest starts
    at `generated`; call `artifact_identity.validate_artifact_bytes()` and
    `transition_state()` per artifact afterwards to promote it.
    """
    from core.database import ArtifactOccurrenceRow, BlobRow, SessionLocal
    from src import artifact_identity as identity

    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('manifest migration batch limit must be between 1 and 500')
    report = {'created': 0, 'already_there': 0, 'would_create': 0, 'next_cursor': None}
    with SessionLocal() as db:
        query = db.query(ArtifactOccurrenceRow)
        if after_id:
            query = query.filter(ArtifactOccurrenceRow.id > after_id)
        rows = query.order_by(ArtifactOccurrenceRow.id).limit(limit).all()
        if len(rows) == limit:
            report['next_cursor'] = rows[-1].id
        for row in rows:
            if identity.latest_manifest(row.id) is not None:
                report['already_there'] += 1
                continue
            if dry_run:
                report['would_create'] += 1
                continue
            blob_row = db.query(BlobRow).filter(BlobRow.sha256 == row.blob_sha256).first()
            identity.record_manifest_version(
                row.id, state='generated',
                format=(blob_row.media_type if blob_row else '') or row.kind,
                byte_size=(blob_row.byte_size if blob_row else None),
                sha256=row.blob_sha256, generator=row.backend or row.skill_id or '',
                reason='backfilled from an existing occurrence without file re-validation')
            report['created'] += 1
    return report


async def startup_copy():
    """Bounded, restartable additive copy; failed rows retry at next startup.

    Readers support historical rows during copying, so this never blocks the
    chat or requires an all-or-nothing migration of a large media library.
    """
    cursor = ''
    while True:
        report = await asyncio.to_thread(copy_legacy, after_id=cursor)
        if not report['next_cursor']:
            return
        cursor = report['next_cursor']
        await asyncio.sleep(0)
