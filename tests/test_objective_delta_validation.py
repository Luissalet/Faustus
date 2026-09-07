import pytest

from services import objectives as obj


@pytest.fixture
def project(tmp_path):
    return {'workspace': str(tmp_path)}


@pytest.mark.parametrize('priority', [True, False, 1.5, 3.9, float('inf'), float('-inf'), float('nan'), [], {}, '2.5'])
@pytest.mark.parametrize('op', ['ADD', 'EDIT'])
def test_bad_priority_does_not_coerce_or_abort_other_deltas(project, priority, op):
    obj.apply_deltas(project, [{'op': 'ADD', 'title': 'Existing'}], 'user')
    result = obj.apply_deltas(project, [
        {'op': op, 'id': 'OBJ-1', 'title': 'Invalid', 'priority': priority},
        {'op': 'ADD', 'title': 'Valid other delta'},
    ], 'user')
    assert len(result['applied']) == 1
    assert 'priority' in result['conflicts'][0]['reason']
    state = obj.load_state(project)
    assert state['objectives']['OBJ-1']['title'] == 'Existing'
    assert len(state['objectives']) == 2


@pytest.mark.parametrize('title', [123, True, {'text': 'Not a title'}, ['Not a title']])
@pytest.mark.parametrize('op', ['ADD', 'EDIT'])
def test_title_must_be_text_not_a_stringified_object(project, title, op):
    obj.apply_deltas(project, [{'op': 'ADD', 'title': 'Existing'}], 'user')
    result = obj.apply_deltas(project, [{'op': op, 'id': 'OBJ-1', 'title': title}], 'user')
    assert not result['applied'] and 'title' in result['conflicts'][0]['reason']


def test_edit_cannot_create_a_duplicate_active_title(project):
    obj.apply_deltas(project, [{'op': 'ADD', 'title': 'First'}, {'op': 'ADD', 'title': 'Second'}], 'user')
    result = obj.apply_deltas(project, [{'op': 'EDIT', 'id': 'OBJ-2', 'title': ' FIRST ', 'priority': 1}], 'user')
    assert not result['applied'] and 'duplicate title' in result['conflicts'][0]['reason']
    stored = obj.load_state(project)['objectives']['OBJ-2']
    assert stored['title'] == 'Second' and stored['priority'] == 3


def test_reopening_dropped_objective_cannot_duplicate_a_replacement(project):
    obj.apply_deltas(project, [{'op': 'ADD', 'title': 'Ship'}], 'user')
    obj.apply_deltas(project, [{'op': 'KILL', 'id': 'OBJ-1'}], 'user')
    obj.apply_deltas(project, [{'op': 'ADD', 'title': 'Ship'}], 'user')
    result = obj.apply_deltas(project, [{'op': 'EDIT', 'id': 'OBJ-1', 'status': 'open'}], 'user')
    assert not result['applied'] and 'duplicate title' in result['conflicts'][0]['reason']
    renamed = obj.apply_deltas(project, [{'op': 'EDIT', 'id': 'OBJ-1', 'status': 'open', 'title': 'Ship v2'}], 'user')
    assert len(renamed['applied']) == 1 and not renamed['conflicts']


@pytest.mark.parametrize('priority', [1, 4, '2', 3.0])
def test_integral_legacy_priorities_remain_compatible(project, priority):
    result = obj.apply_deltas(project, [{'op': 'ADD', 'title': 'Valid', 'priority': priority}], 'user')
    assert not result['conflicts'] and result['state']['objectives'][0]['priority'] == int(priority)
