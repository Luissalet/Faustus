from dataclasses import replace

import pytest

from src.state_mirror import contracts as C, ingest, persistence
from src.state_mirror.conflicts import converged


OWNER = 'alice'
ENTITY = C.entity_id('run', OWNER, 'test')


def at(second):
    return f'2026-09-07T12:00:{second:02d}Z'


def sample(source, value, second, *, epistemic='observed', owner=OWNER):
    return C.StateObservation.parse({'entity_id': C.entity_id('run', owner, 'test'),
        'owner': owner, 'source': source, 'schema': 'run_state.v1', 'epistemic': epistemic,
        'observed_at': at(second), 'state': {'status': value}})


@pytest.fixture
def store(tmp_path):
    result = persistence.StateStore(path=str(tmp_path / 'state.db'))
    yield result
    result.close()


def fold(store, *samples, now=5, publisher=None):
    result = ingest.ingest(samples, store=store, now=at(now), publisher=publisher)
    assert result.errors == ()
    assert result.refused == ()
    return result


def disagree(store):
    fold(store, sample('a', 'running', 0), sample('b', 'failed', 1))
    return store.conflicts(owner=OWNER)[0]


def test_repeated_disagreement_has_one_stored_identity(store):
    conflict = disagree(store)
    fold(store, sample('a', 'running', 2), sample('b', 'failed', 3))
    assert [c.id for c in store.conflicts(owner=OWNER)] == [conflict.id]
    assert store.get_state(ENTITY).conflicts == (conflict.id,)


def test_both_sources_recheck_and_agree_closes_once_and_preserves_history(store):
    conflict = disagree(store)
    events = []
    class Recorder:
        def publish(self, name, **payload):
            events.append((name, payload))
    recorder = Recorder()
    fold(store, sample('a', 'done', 2), publisher=recorder)
    assert len(store.conflicts(owner=OWNER)) == 1
    fold(store, sample('b', 'done', 3), publisher=recorder)
    assert store.conflicts(owner=OWNER) == []
    archived = store.conflicts(owner=OWNER, open_only=False)
    assert archived[0].id == conflict.id
    assert archived[0].status == 'resolved'
    assert 'a, b' in archived[0].resolution
    assert len(store.observations(ENTITY)) == 4
    fold(store, sample('b', 'done', 3), publisher=recorder)
    assert [name for name, _ in events].count('state_conflict_resolved') == 1


def test_one_source_rechecking_is_not_agreement(store):
    disagree(store)
    fold(store, sample('a', 'failed', 2))
    assert store.conflicts(owner=OWNER)


def test_late_arriving_old_observation_cannot_hide_new_disagreement(store):
    disagree(store)
    fold(store, sample('a', 'done', 2), sample('b', 'failed', 4), sample('b', 'done', 3))
    assert store.conflicts(owner=OWNER)
    latest = {s.source: s for s in store.latest_field_observations(ENTITY, 'status')}
    assert latest['b'].state['status'] == 'failed'


def test_third_source_disagreement_is_not_silently_resolved(store):
    disagree(store)
    fold(store, sample('c', 'failed', 2), sample('a', 'done', 3), sample('b', 'done', 4))
    assert store.conflicts(owner=OWNER)
    fold(store, sample('c', 'done', 5), now=6)
    assert store.conflicts(owner=OWNER) == []


@pytest.mark.parametrize('case', ['stale', 'future', 'weak', 'missing', 'foreign', 'typed', 'too_many'])
def test_insufficient_evidence_never_resolves(store, case):
    conflict = disagree(store)
    observations = [sample('a', 'done', 2), sample('b', 'done', 3)]
    state = replace(store.get_state(ENTITY), fields={'status': C.FieldState(value='done')})
    now = at(5)
    if case == 'stale':
        now = '2026-09-07T13:00:00Z'
    elif case == 'future':
        observations[1] = sample('b', 'done', 6)
    elif case == 'weak':
        observations[1] = sample('b', 'done', 3, epistemic='inferred')
    elif case == 'missing':
        observations[1] = replace(observations[1], state={})
    elif case == 'foreign':
        observations[1] = sample('b', 'done', 3, owner='bob')
    elif case == 'typed':
        observations = [replace(o, state={'status': True if o.source == 'a' else 1}) for o in observations]
        state = replace(state, fields={'status': C.FieldState(value=True)})
    else:
        observations *= 17
    assert not converged(conflict, observations, state, now=now)


def test_concurrent_observation_fences_resolution(store, monkeypatch):
    disagree(store)
    fold(store, sample('a', 'done', 2))
    original = store.settle_conflict
    def raced(conflict_id, **kwargs):
        store.append_observation(sample('a', 'failed', 4))
        return original(conflict_id, **kwargs)
    monkeypatch.setattr(store, 'settle_conflict', raced)
    fold(store, sample('b', 'done', 3))
    assert store.conflicts(owner=OWNER)


def test_source_times_are_ordered_as_instants_not_lexicographic_strings(store):
    first = sample('a', 'done', 1)
    old = replace(sample('a', 'failed', 0), observed_at='2026-09-07T14:00:00+02:00')
    store.append_observation(first)
    store.append_observation(old)
    assert store.latest_field_observations(ENTITY, 'status')[0].id == first.id
