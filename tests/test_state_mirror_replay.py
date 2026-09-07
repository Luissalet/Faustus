from dataclasses import replace

import pytest

from src.state_mirror import replay
from tests.test_state_mirror_conflict_resolution import store, sample, fold, ENTITY, OWNER  # noqa: F401


def test_rebuild_restores_full_state_after_observation_retention(store):
    fold(store, sample('a', 'running', 0), sample('b', 'failed', 1))
    expected = store.get_state(ENTITY).to_dict()
    store.compact(keep_per_entity=1)
    receipt = store.rebuild_state(ENTITY, owner=OWNER)
    assert receipt['matches'] and not receipt['repaired']
    with store._db() as conn:
        conn.execute('DELETE FROM state_materialized WHERE entity_id=?', [ENTITY])
    result = store.rebuild_state(ENTITY, owner=OWNER, apply=True,
                                 expected_sha256=receipt['receipt']['sha256'])
    assert result['repaired']
    assert store.get_state(ENTITY).to_dict() == expected


def test_checkpoint_keeps_full_state_and_bounded_tail(store, monkeypatch):
    monkeypatch.setattr(replay, 'KEEP_STEPS', 3)
    for second in range(8):
        fold(store, sample('a', 'running', second), now=second)
    result = store.rebuild_state(ENTITY, owner=OWNER)
    assert result['matches']
    assert result['receipt']['baseline_sequence'] >= 6
    assert result['receipt']['steps_verified'] < 3


@pytest.mark.parametrize('damage', ['patch', 'missing', 'checkpoint', 'head'])
def test_corrupt_journal_never_overwrites_materialized_state(store, damage):
    fold(store, sample('a', 'running', 0))
    original = store.get_state(ENTITY)
    with store._db() as conn:
        if damage == 'patch':
            conn.execute("UPDATE state_replay_steps SET patch='{}'")
        elif damage == 'missing':
            conn.execute('DELETE FROM state_replay_steps')
        elif damage == 'checkpoint':
            conn.execute("UPDATE state_replay_heads SET base='{}', base_hash='wrong'")
        else:
            conn.execute("UPDATE state_replay_heads SET head_hash='wrong'")
    with pytest.raises((ValueError, KeyError)):
        store.rebuild_state(ENTITY, owner=OWNER, apply=True, expected_sha256='wrong')
    assert store.get_state(ENTITY) == original


def test_stale_receipt_and_foreign_owner_cannot_rebuild(store):
    from src.state_mirror.persistence import NotFound
    fold(store, sample('a', 'running', 0))
    old = store.rebuild_state(ENTITY, owner=OWNER)['receipt']['sha256']
    fold(store, sample('a', 'done', 1))
    with pytest.raises(ValueError, match='changed'):
        store.rebuild_state(ENTITY, owner=OWNER, apply=True, expected_sha256=old)
    for owner in ['bob', '']:
        with pytest.raises(NotFound):
            store.rebuild_state(ENTITY, owner=owner)


def test_corrupt_read_model_requires_repair_before_new_write(store):
    fold(store, sample('a', 'running', 0))
    original = store.get_state(ENTITY)
    with store._db() as conn:
        conn.execute('UPDATE state_materialized SET revision=999')
    with pytest.raises(ValueError, match='differs'):
        store.put_state(replace(original, revision=2))
    preview = store.rebuild_state(ENTITY, owner=OWNER)
    assert not preview['matches']
    store.rebuild_state(ENTITY, owner=OWNER, apply=True, expected_sha256=preview['receipt']['sha256'])
    assert store.get_state(ENTITY) == original


def test_legacy_state_is_explicit_baseline_not_fabricated_observation_history(store):
    fold(store, sample('a', 'running', 0))
    with store._db() as conn:
        conn.execute('DELETE FROM state_replay_heads')
        conn.execute('DELETE FROM state_replay_steps')
    with pytest.raises(ValueError, match='no verified'):
        store.rebuild_state(ENTITY, owner=OWNER)
    fold(store, sample('a', 'done', 1))
    assert store.rebuild_state(ENTITY, owner=OWNER)['receipt']['origin'] == 'legacy_checkpoint'
