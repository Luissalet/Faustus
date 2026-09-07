from dataclasses import replace

import pytest

from src.state_mirror.contracts import StateObservation
from src.state_mirror.reducers import reduce_observation

STAMP = '2026-09-07T12:00:10Z'


def observation(**overrides):
    return StateObservation(**dict({
        'id': 'obs-fixture', 'entity_id': 'device-fixture', 'owner': 'alice',
        'schema': 'device_state.v1', 'source': 'probe-a',
        'observed_at': STAMP, 'valid_for_seconds': 60,
        'state': {'cpu_percent': 25, 'ram_used_bytes': 100},
    }, **overrides))


def test_late_complete_snapshot_cannot_erase_newer_fields():
    before = reduce_observation(None, observation(), now=STAMP).state
    late = observation(id='obs-late', observed_at='2026-09-07T12:00:01Z', partial=False, state={})
    result = reduce_observation(before, late, now=STAMP)
    assert result.state.values() == before.values()
    assert not result.changes and result.state.revision == before.revision


def test_weaker_complete_snapshot_cannot_erase_fresh_measurements():
    before = reduce_observation(None, observation(), now=STAMP).state
    weak = observation(id='obs-inferred', observed_at='2026-09-07T12:00:11Z',
                       epistemic='inferred', partial=False, state={})
    result = reduce_observation(before, weak, now='2026-09-07T12:00:12Z')
    assert result.state.values() == before.values()
    assert not result.changes


def test_newer_measured_complete_snapshot_can_remove_fields():
    before = reduce_observation(None, observation(), now=STAMP).state
    current = observation(id='obs-now', observed_at='2026-09-07T12:00:11Z', partial=False,
                          state={'cpu_percent': 26})
    result = reduce_observation(before, current, now='2026-09-07T12:00:12Z')
    assert result.state.values() == {'cpu_percent': 26}
    assert {c.field for c in result.changes} == {'cpu_percent', 'ram_used_bytes'}


def test_direct_reducer_cannot_mix_owners():
    original = observation(project_id='project-a')
    before = reduce_observation(None, original, now=STAMP).state
    foreign = replace(original, owner='bob', state={'cpu_percent': 99})
    result = reduce_observation(before, foreign, now=STAMP)
    assert result.refusal and not result.applied
    assert result.state == before


def test_shared_device_remains_observable_from_another_project_scope():
    original = observation(project_id='project-a')
    before = reduce_observation(None, original, now=STAMP).state
    current = replace(original, id='obs-project-b', project_id='project-b', state={'cpu_percent': 99})
    result = reduce_observation(before, current, now=STAMP)
    assert not result.refusal and result.state.values()['cpu_percent'] == 99


def test_same_conflict_reduces_to_identical_ids_and_materialized_state():
    original = observation()
    # A valid structured id is needed by StateConflict.parse.
    from src.state_mirror.contracts import entity_id
    original = replace(original, entity_id=entity_id('device', 'alice', 'machine'))
    before = reduce_observation(None, original, now=STAMP).state
    disagreement = replace(original, id='obs-other', source='probe-b', state={'cpu_percent': 99})
    first = reduce_observation(before, disagreement, now=STAMP)
    second = reduce_observation(before, disagreement, now=STAMP)
    assert first.conflicts and first.state == second.state
    assert first.conflicts == second.conflicts
