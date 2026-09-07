"""Production cutover: old links, new ownership, bounded additive migration."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import database as db_mod
from core.database import ArtifactRow, ArtifactOccurrenceRow, BlobRow
from src import artifact_store as store, artifact_identity as identity
from src.artifact_migration import copy_legacy
from src.contracts import ExecutionResult
from src.project_context.models import SourceRef
from src.project_context.resolvers.artifact import ArtifactResolver
from tests.test_artifact_store import own_database  # noqa: F401


@pytest.fixture()
def world(own_database, tmp_path, monkeypatch):
    destination = tmp_path / 'artifacts'
    gallery = tmp_path / 'gallery'
    destination.mkdir()
    gallery.mkdir()
    monkeypatch.setattr(store, 'ARTIFACT_STORE_DIR', str(destination))
    from src import constants
    monkeypatch.setattr(constants, 'GENERATED_IMAGES_DIR', str(gallery))
    return destination, gallery


def legacy(world, ident='old-1', *, gallery=False, filename=None, digest=None):
    destination, source_gallery = world
    body = b'# A useful report\n'
    import hashlib
    measured = hashlib.sha256(body).hexdigest()
    name = filename or (ident + '.md' if gallery else measured + '.md')
    (source_gallery if gallery else destination).joinpath(name).write_bytes(body)
    with db_mod.SessionLocal() as db:
        db.add(ArtifactRow(id=ident, kind='document', filename=name,
                           sha256=digest if digest is not None else (None if gallery else measured),
                           owner='alice', project_id='p1', run_id='r1', session_id='s1',
                           label='Historical report', model='local-model',
                           model_license='MIT', byte_size=len(body),
                           legacy_gallery_id='gallery-' + ident if gallery else None))
        db.commit()
    return name, measured


def test_copy_preserves_alias_owner_provenance_and_old_row(world):
    destination, gallery = world
    name, digest = legacy(world)
    report = copy_legacy()
    assert report['created'] == 1 and not report['skipped']
    found = identity.resolve('old-1')
    assert found.owner == 'alice' and found.session_id == 's1'
    assert found.provenance.model_license == 'MIT'
    assert Path(identity.path_for('old-1', owner='alice')).read_bytes().startswith(b'#')
    with pytest.raises(identity.NotTheOwner):
        identity.path_for('old-1', owner='bob')
    assert copy_legacy()['created'] == 0
    assert identity.reference_count(digest) == 1
    with db_mod.SessionLocal() as db:
        assert db.get(ArtifactRow, 'old-1') is not None


def test_hashless_gallery_is_copied_not_moved_and_resolves_before_and_after(world):
    destination, gallery = world
    name, digest = legacy(world, gallery=True)
    resolver = ArtifactResolver(store_dir=str(destination))
    ref = SourceRef(kind='artifact', id='old-1')
    assert resolver.metadata(ref, owner='alice', project={'id': 'p1'}).state == 'ok'
    before = resolver.read(ref).text
    assert 'useful report' in before
    assert copy_legacy()['created'] == 1
    assert (gallery / name).exists()
    assert identity.resolve('old-1').blob_sha256 == digest
    assert resolver.read(ref).text == before
    assert resolver.metadata(ref, owner='bob', project={'id': 'p1'}).state == 'forbidden'
    assert resolver.metadata(ref, owner='alice', project={'id': 'p2'}).state == 'forbidden'


def test_batches_progress_past_missing_and_corrupt_rows(world):
    name, _ = legacy(world, ident='01', filename='missing.md')
    world[0].joinpath(name).unlink()
    legacy(world, ident='02', digest='0' * 64)
    legacy(world, ident='03', gallery=True)
    first = copy_legacy(limit=2)
    assert len(first['skipped']) == 2 and first['next_cursor'] == '02'
    second = copy_legacy(limit=2, after_id=first['next_cursor'])
    assert second['created'] == 1 and second['next_cursor'] is None
    assert identity.resolve('01') is None and identity.resolve('02') is None


def test_new_occurrences_are_visible_to_project_context_and_catalog(world, tmp_path):
    from src.artifact_catalog import recent
    ids = []
    for owner in ('alice', 'bob'):
        source = tmp_path / owner
        source.mkdir()
        (source / 'report.md').write_text('# Shared bytes', encoding='utf-8')
        result = ExecutionResult.parse({'run_id': owner + '-run', 'backend': 'docker_workspace',
                                        'status': 'completed', 'artifact_filenames': ['report.md']})
        collected = store.collect(result, source_dir=str(source), owner=owner, project_id='p1')
        store.persist(collected.artifacts)
        ids.append(collected.artifacts[0].id)
    assert ids[0] != ids[1] and len(list(world[0].iterdir())) == 1
    resolver = ArtifactResolver()
    ref = SourceRef(kind='artifact', id=ids[0])
    assert resolver.metadata(ref, owner='alice', project={'id': 'p1'}).state == 'ok'
    assert resolver.read(ref).text == '# Shared bytes'
    with db_mod.SessionLocal() as db:
        assert [r.id for r in recent(db, owner='alice')] == [ids[0]]
        assert db.query(ArtifactRow).count() == 0
        assert db.query(ArtifactOccurrenceRow).count() == 2
        assert db.query(BlobRow).one().refcount == 2


def test_atomic_publication_has_one_winner_and_no_temporary_left(world, tmp_path):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'x' * 10000)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.publish_copy(str(source), str(world[0]), 'x.txt'), range(8)))
    assert sum(result[3] for result in results) == 1
    assert len(list(world[0].iterdir())) == 1
    assert source.exists()


def test_corrupt_existing_target_is_never_overwritten(world, tmp_path):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'original')
    _, _, filename, _ = store.publish_copy(str(source), str(world[0]), 'x.txt')
    target = world[0] / filename
    target.write_bytes(b'changed')
    with pytest.raises(ValueError, match='content hash'):
        store.publish_copy(str(source), str(world[0]), 'x.txt')
    assert target.read_bytes() == b'changed'
    assert not list(world[0].glob('.collect-*'))


def test_copy_budget_leaves_source_intact_and_no_partial_blob(world, tmp_path):
    source = tmp_path / 'large.bin'
    source.write_bytes(b'12345')
    with pytest.raises(ValueError, match='byte limit'):
        store.publish_copy(str(source), str(world[0]), 'large.bin', max_bytes=4)
    assert source.read_bytes() == b'12345' and not list(world[0].iterdir())


def test_store_rejects_windows_alternate_streams(world):
    with pytest.raises(ValueError, match='bare artifact name'):
        store.path_of('report.md:secret')


def test_concurrent_migrations_preserve_one_occurrence_and_reference(world):
    _, digest = legacy(world)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: copy_legacy(), range(4)))
    assert sum(result['created'] for result in results) == 1
    assert identity.reference_count(digest) == 1
    assert all(not result['skipped'] for result in results)


def test_copy_preserves_preview_and_access_is_through_owner(world):
    legacy(world)
    (world[0] / 'preview.png').write_bytes(b'preview bytes')
    with db_mod.SessionLocal() as db:
        db.get(ArtifactRow, 'old-1').preview_filename = 'preview.png'
        db.commit()
    assert copy_legacy()['created'] == 1
    assert identity.derivatives_for('old-1', owner='alice')[0].filename == 'preview.png'
    with pytest.raises(identity.NotTheOwner):
        identity.derivatives_for('old-1', owner='bob')


def test_removing_migrated_output_does_not_resurrect_it_through_old_row(world):
    from src import artifact_catalog
    legacy(world)
    copy_legacy()
    found = identity.resolve('old-1')
    assert identity.forget_occurrence(found.id)['removed']
    assert copy_legacy()['created'] == 0
    with db_mod.SessionLocal() as db:
        assert artifact_catalog.get(db, 'old-1') is None
        assert artifact_catalog.get(db, found.id) is None
        assert artifact_catalog.recent(db, owner='alice') == []
        assert db.get(ArtifactRow, 'old-1') is not None
    with pytest.raises(ValueError, match='removed'):
        identity.record_occurrence(found)
    with pytest.raises(RuntimeError, match='discard'):
        db_mod.rollback_artifact_identity_tables()
