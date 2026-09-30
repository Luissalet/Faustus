"""Strict standing-memory projections use one read transaction, never repair."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from src import memory_engine as engine, memory_conflicts as conflicts

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(engine, 'memory_conflict_detection_enabled', lambda: True)
    engine.set_vector_store(None)
    with engine._db():
        pass
    with conflicts._db():
        pass
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    yield tmp_path
    engine.reset_vector_store()


def add(text='Always run QA tests', owner='qa', project='workspace'):
    return engine.add_item(text, owner=owner, project=project, level='procedural',
                           trust_class='human_explicit', now=NOW)


def freeze_writers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('strict snapshot invoked a writer/initializer')
    for attr in ('_db', '_open', '_connect', '_quarantine', 'vector_store', 'touch'):
        monkeypatch.setattr(engine, attr, forbidden)
    monkeypatch.setattr(conflicts, '_db', forbidden)


def test_unchanged_real_snapshot_matches_legacy_projection_without_writes(database, monkeypatch):
    row = add()
    expected = engine.public_item(engine.scoped_items('qa', 'workspace')[0], NOW)
    # Legacy writer initializes WAL; close it and select DELETE for a strict
    # byte/files proof that does not mistake SQLite WAL sidecars for row writes.
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    path = Path(engine.db_path())
    before = path.read_bytes()
    files = sorted(p.name for p in database.iterdir())
    freeze_writers(monkeypatch)
    first = engine.context_snapshot('qa', 'workspace', now=NOW)
    second = engine.context_snapshot('qa', 'workspace', now=NOW)
    assert first == second == [expected]
    assert first[0]['id'] == row['id'] and first[0]['access_count'] == 0
    assert path.read_bytes() == before
    assert sorted(p.name for p in database.iterdir()) == files


def test_owner_project_filters_include_only_their_unscoped_rows(database):
    visible = add('QA visible')
    global_row = add('QA global', owner='', project='')
    add('QA other owner', owner='other')
    add('QA other workspace', project='different')
    rows = engine.context_snapshot('qa', 'workspace', now=NOW)
    assert {row['id'] for row in rows} == {visible['id'], global_row['id']}
    assert [row['id'] for row in engine.context_snapshot('', '', now=NOW)] == [global_row['id']]
    with pytest.raises(ValueError):
        engine.context_snapshot(None, 'workspace')


def test_empty_then_new_and_same_timestamp_content_change(database):
    assert engine.context_snapshot('qa', 'workspace', now=NOW) == []
    row = add()
    first = engine.context_snapshot('qa', 'workspace', now=NOW)
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute('UPDATE items SET text=? WHERE id=?', ('Always run changed QA tests', row['id']))
    second = engine.context_snapshot('qa', 'workspace', now=NOW)
    assert first[0]['updated_at'] == second[0]['updated_at']
    assert first[0]['text'] != second[0]['text']


def test_conflict_change_is_visible_in_same_connection(database, monkeypatch):
    row = add()
    first = engine.context_snapshot('qa', 'workspace', now=NOW)[0]
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute("INSERT INTO memory_conflicts (id,owner,new_id,old_id,status) VALUES (?,?,?,?,?)",
                     ('qa-conflict','qa','new',row['id'],'open'))
    freeze_writers(monkeypatch)
    second = engine.context_snapshot('qa', 'workspace', now=NOW)[0]
    assert second['effective_score'] == first['effective_score'] * conflicts.RANKING_PENALTY
    assert 'contradicted by a newer memory' in second['text']
    assert second['open_conflict']['id'] == 'qa-conflict'


@pytest.mark.parametrize('nested', [False, True])
def test_missing_path_never_creates_file_or_parent(tmp_path, monkeypatch, nested):
    target = tmp_path/'absent' if nested else tmp_path
    monkeypatch.setattr(engine, 'DATA_DIR', str(target))
    with pytest.raises(sqlite3.Error):
        engine.context_snapshot('qa', 'workspace')
    assert not Path(engine.db_path()).exists()
    if nested:
        assert not target.exists()


def test_corrupt_sqlite_never_quarantines_or_repairs(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'DATA_DIR', str(tmp_path))
    path = Path(engine.db_path()); path.write_bytes(b'not sqlite QA bytes')
    freeze_writers(monkeypatch)
    with pytest.raises(sqlite3.DatabaseError):
        engine.context_snapshot('qa', 'workspace')
    assert path.read_bytes() == b'not sqlite QA bytes'
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize('table', ['items', 'memory_conflicts'])
def test_missing_schema_is_unknown_even_when_empty(database, table):
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute('DROP TABLE '+table)
    with pytest.raises(sqlite3.OperationalError):
        engine.context_snapshot('qa', 'workspace')


def test_conflict_failure_propagates_only_in_strict_mode(database):
    row = add()
    conn = sqlite3.connect(engine.db_path()); conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conflicts.open_conflict_for(row['id'], 'qa', connection=conn, strict=True)
    assert conflicts.open_conflict_for(row['id'], 'qa', connection=conn) is None
    with pytest.raises(ValueError):
        conflicts.open_conflict_for(row['id'], 'qa', strict=True)


def test_connection_failure_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'DATA_DIR', str(tmp_path))
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic connect failure')
    monkeypatch.setattr(sqlite3, 'connect', failed)
    with pytest.raises(sqlite3.OperationalError, match='synthetic connect failure'):
        engine.context_snapshot('qa', 'workspace')


def test_query_only_transaction_and_one_connection(database, monkeypatch):
    add()
    real_connect = sqlite3.connect
    calls, statements = [], []
    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, 'connect', observed)
    assert engine.context_snapshot('qa', 'workspace', now=NOW)
    assert len(calls) == 1 and calls[0][1]['uri'] is True
    assert calls[0][0][0].endswith('?mode=ro')
    assert statements[:2] == ['PRAGMA query_only=ON', 'BEGIN']
    assert all(statement.upper().startswith(('SELECT', 'PRAGMA QUERY_ONLY', 'BEGIN')) for statement in statements)


def test_conflict_rows_share_items_snapshot_under_concurrent_commit(database, monkeypatch):
    row = add()
    # WAL permits a concurrent committed writer while the read transaction
    # keeps rows and conflicts at its first SELECT's snapshot.
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    original = engine.public_item
    changed = []
    def concurrent(item, now=None, **kwargs):
        if not changed:
            with sqlite3.connect(engine.db_path()) as conn:
                conn.execute("INSERT INTO memory_conflicts(id,owner,new_id,old_id,status) VALUES(?,?,?,?,?)",
                             ('late-conflict','qa','new',row['id'],'open'))
            changed.append(True)
        return original(item, now, **kwargs)
    monkeypatch.setattr(engine, 'public_item', concurrent)
    first = engine.context_snapshot('qa', 'workspace', now=NOW)[0]
    assert first['open_conflict'] is None
    assert engine.context_snapshot('qa', 'workspace', now=NOW)[0]['open_conflict']['id'] == 'late-conflict'


def test_closed_writer_wal_reader_can_create_operational_sidecars(database):
    row = add()
    path = Path(engine.db_path())
    # add's normal writer enables WAL and its last close removes idle sidecars.
    assert sorted(p.name for p in database.iterdir()) == [path.name]
    before = path.read_bytes()
    snapshot = engine.context_snapshot('qa', 'workspace', now=NOW)
    assert snapshot[0]['id'] == row['id'] and snapshot[0]['access_count'] == 0
    assert path.read_bytes() == before  # Header and stored application bytes too.
    assert sorted(p.name for p in database.iterdir()) == [path.name, path.name+'-shm', path.name+'-wal']
    assert engine.context_snapshot('qa', 'workspace', now=NOW) == snapshot


def test_readonly_snapshot_sees_latest_commit_in_live_wal(database):
    row = add()
    path = Path(engine.db_path())
    writer = sqlite3.connect(path)
    try:
        writer.execute('UPDATE items SET text=? WHERE id=?', ('Always run latest committed QA', row['id']))
        writer.commit()
        assert Path(str(path)+'-wal').stat().st_size > 0
        before = path.read_bytes()
        snapshot = engine.context_snapshot('qa', 'workspace', now=NOW)
        assert snapshot[0]['text'] == 'Always run latest committed QA'
        assert snapshot[0]['updated_at'] == row['updated_at']
        assert snapshot[0]['access_count'] == 0
        assert path.read_bytes() == before
        assert writer.execute('SELECT text,access_count FROM items WHERE id=?', (row['id'],)).fetchone() == (
            'Always run latest committed QA', 0)
    finally:
        writer.close()
