"""Owner-scoped access to generated outputs, including historical aliases."""
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src import artifact_catalog


def _owner(request):
    # Authentication middleware supplies the principal. Reading one's own
    # output is not an administrative capability.
    from core import middleware
    from src.owner_identity import effective_storage_owner
    owner = effective_storage_owner(getattr(request.state, 'current_user', None),
                                    auth_is_disabled=middleware.auth_disabled())
    if not owner:
        raise HTTPException(401, 'An authenticated artifact owner is required')
    return owner


def _metadata(row):
    return {'id': row.id, 'kind': row.kind, 'label': row.label or row.filename,
            'sha256': row.sha256, 'byte_size': row.byte_size,
            'media_type': row.media_type, 'project_id': row.project_id or '',
            'run_id': row.run_id or '', 'session_id': row.session_id or '',
            'partial': bool(row.partial), 'created_at': str(row.created_at),
            'download_url': '/api/artifacts/' + row.id + '/download'}


def setup_artifact_routes():
    router = APIRouter(prefix='/api/artifacts', tags=['artifacts'])

    @router.get('')
    def list_artifacts(request: Request, project_id: str = '', session_id: str = '',
                       kind: str = '', q: str = '', since: str = '', until: str = '',
                       limit: int = 80):
        """ART-08: the Library's one search surface across every artifact
        kind — ``kind``/``session_id``/``q``/``since``/``until`` are optional
        facets on top of the original ``project_id``+``limit`` call."""
        owner = _owner(request)
        if not 1 <= limit <= 200:
            raise HTTPException(400, 'limit must be between 1 and 200')
        from core.database import SessionLocal
        try:
            with SessionLocal() as db:
                rows = artifact_catalog.recent(
                    db, owner=owner, project_id=project_id, session_id=session_id,
                    kind=kind, q=q, since=since, until=until, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {'ok': True, 'artifacts': [_metadata(row) for row in rows]}

    def owned(request, artifact_id):
        owner = _owner(request)
        from core.database import SessionLocal
        with SessionLocal() as db:
            row = artifact_catalog.get(db, artifact_id)
            if row is None or (row.owner or '') != owner:
                raise HTTPException(404, 'Artifact not found')
            return row

    @router.get('/{artifact_id}')
    def metadata(request: Request, artifact_id: str):
        return {'ok': True, 'artifact': _metadata(owned(request, artifact_id))}

    @router.get('/{artifact_id}/manifest')
    def manifest_history(request: Request, artifact_id: str):
        """The full version chain (ART-01): what state each pass left it in
        and why, oldest first. Empty for anything written before this lot
        migrated it — `src.artifact_migration.migrate_manifests()` backfills
        that, not this endpoint."""
        row = owned(request, artifact_id)
        from src import artifact_identity as identity
        return {'ok': True, 'artifact_id': row.id,
               'manifest': identity.manifest_history(row.id)}

    @router.post('/{artifact_id}/review')
    def mark_reviewed(request: Request, artifact_id: str):
        """Human sign-off. Only reachable from `validated`: an artifact whose
        format check never ran, or failed, cannot be marked reviewed by
        calling this — the state machine in `artifact_identity` refuses the
        jump and this reports why instead of forcing it through."""
        row = owned(request, artifact_id)
        from src import artifact_identity as identity
        try:
            updated = identity.transition_state(row.id, 'reviewed',
                                                reason='marked reviewed via API')
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {'ok': True, 'manifest': updated}

    @router.get('/{artifact_id}/provenance')
    def provenance(request: Request, artifact_id: str):
        """ART-05: what produced this artifact, so a numeric claim can link
        back to the exact script and input bytes instead of being taken on
        faith — `recipe_fingerprint`/`inputs_digest` are the hashes
        `src.artifact_store.analysis_provenance()` computed when this was
        collected; `source_artifact_ids` lets an input itself be a prior
        artifact, so the chain is walkable via this same endpoint."""
        import json as _json
        row = owned(request, artifact_id)
        try:
            source_ids = _json.loads(row.source_artifact_ids) if getattr(row, 'source_artifact_ids', None) else []
        except (TypeError, ValueError):
            source_ids = []
        return {'ok': True, 'artifact_id': row.id,
               'provenance': {
                   'backend': getattr(row, 'backend', None),
                   'recipe': getattr(row, 'recipe', None),
                   'recipe_fingerprint': getattr(row, 'recipe_fingerprint', None),
                   'inputs_digest': getattr(row, 'inputs_digest', None),
                   'source_artifact_ids': source_ids,
                   'note': getattr(row, 'provenance_note', '') or '',
               }}

    @router.delete('/{artifact_id}')
    def forget(request: Request, artifact_id: str):
        """ART-08: remove this owner's context link to an artifact without
        touching bytes another occurrence (this owner's or anyone else's)
        still shares — src.artifact_identity.forget_occurrence() already
        keeps that promise; this only exposes it, scoped to the caller's own
        occurrence via the same `owned()` ownership check every other route
        here uses."""
        row = owned(request, artifact_id)
        from src import artifact_identity as identity
        result = identity.forget_occurrence(row.id)
        return {'ok': bool(result.get('removed')), 'references_left': result.get('references_left', 0)}

    @router.get('/{artifact_id}/download')
    def download(request: Request, artifact_id: str):
        row = owned(request, artifact_id)
        try:
            filename = artifact_catalog.path(row)
        except (ValueError, TypeError):
            raise HTTPException(404, 'Artifact file unavailable')
        if not os.path.isfile(filename):
            raise HTTPException(404, 'Artifact file unavailable')
        # HTML/SVG outputs are downloads, never active code under the app origin.
        label = os.path.basename((row.label or row.filename).replace('\\', '/'))
        label = ''.join(c for c in label if c >= ' ' and c != '\x7f')[:200] or 'artifact'
        return FileResponse(filename, media_type=row.media_type or 'application/octet-stream',
                            filename=label, headers={'X-Content-Type-Options': 'nosniff',
                            'Content-Security-Policy': "sandbox; default-src 'none'",
                            'Cache-Control': 'private, no-store'})

    return router
