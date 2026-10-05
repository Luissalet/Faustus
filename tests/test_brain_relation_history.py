"""Recorded time, valid time, forgetting and owner-scoped graph retrieval."""
import pytest
from src.brain import db as brain_db, entities, relation_history as history
from src.brain.temporal_graph import GraphQuery, retrieve


@pytest.fixture
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / 'brain'))
    clock = ['2026-01-01T00:00:00.000000Z']
    monkeypatch.setattr(history, 'recorded_now', lambda: clock[0])
    yield clock
    brain_db.use_dir(None)


def seed():
    ada = entities.upsert_entity('alice', 'Ada', type='person')
    lab = entities.upsert_entity('alice', 'Lab', type='org')
    r = entities.add_relation('alice', ada['id'], 'works_at', dst_id=lab['id'],
                              valid_from='2024-01-01', evidence=['private:one', 'private:two'])
    return ada, lab, r


def test_valid_time_and_recorded_time_are_independent(store):
    ada, lab, old = seed()
    store[0] = '2026-02-01T00:00:00.000000Z'
    new_lab = entities.upsert_entity('alice', 'New Lab', type='org')
    entities.add_relation('alice', ada['id'], 'works_at', dst_id=new_lab['id'], valid_from='2025-01-01')
    before = entities.list_relations('alice', as_of='2025-06-01', known_at='2026-01-15')
    after = entities.list_relations('alice', as_of='2025-06-01', known_at='2026-02-15')
    assert [r['dst'] for r in before] == [lab['id']]
    assert [r['dst'] for r in after] == [new_lab['id']]
    assert entities.list_relations('alice', known_at='2025-12-01') == []
    assert history.horizon('alice') == '2026-01-01T00:00:00.000000Z'


def test_forgetting_scrubs_all_old_evidence_and_physical_delete(store):
    _, _, relation = seed()
    store[0] = '2026-02-01T00:00:00.000000Z'
    entities.forget_source('alice', 'private:one')
    before = history.at('alice', '2026-01-15')
    assert before[0]['evidence'] == ['private:two']
    with brain_db.db() as conn:
        conn.execute('DELETE FROM relations WHERE id=?', (relation['id'],))
    assert history.at('alice', '2026-01-15') == []


def test_history_rolls_back_with_relation_change(store):
    _, _, relation = seed()
    store[0] = '2026-02-01T00:00:00.000000Z'
    with pytest.raises(RuntimeError):
        with brain_db.db() as conn:
            conn.execute("UPDATE relations SET dst_value='ghost' WHERE id=?", (relation['id'],))
            raise RuntimeError('rollback')
    assert history.at('alice', '2026-02-15')[0]['dst_value'] == ''


def test_graph_owner_visibility_limits_and_invalid_dates(store):
    ada, lab, relation = seed()
    assert retrieve('bob', GraphQuery(entity_id=ada['id']))['nodes'] == []
    small = retrieve('alice', GraphQuery(entity_id=ada['id'], limit=1))
    assert len(small['nodes']) == 1 and not small['edges'] and small['truncated']
    entities.set_hidden(lab['id'], True)
    result = retrieve('alice', GraphQuery(entity_id=ada['id'], known_at='2026-01-15'))
    assert not result['edges'] and len(result['nodes']) == 1
    with pytest.raises(ValueError):
        retrieve('alice', GraphQuery(known_at='yesterday'))


def test_legacy_snapshot_starts_at_migration_not_fact_validity(store):
    seed()
    with brain_db.db() as conn:
        conn.execute('DELETE FROM relation_versions')
    store[0] = '2026-03-01T00:00:00.000000Z'
    assert history.at('alice', '2026-02-01') == []
    assert history.horizon('alice') == store[0]
    assert len(history.at('alice', '2026-03-02')) == 1
