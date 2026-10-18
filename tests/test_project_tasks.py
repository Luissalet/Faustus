import copy
import pytest
from src import project_tasks as tasks


@pytest.fixture
def ledger(monkeypatch):
    state = {'spaces': [{'id': 'silver', 'project_id': 'p', 'title': 'Silver'}], 'tasks': {}, 'calls': []}
    def call(tool, args):
        state['calls'].append((tool, copy.deepcopy(args)))
        if tool == 'hub_agora_spaces':
            return {'ok': True, 'spaces': state['spaces']}
        assert args['space_id'] == 'silver'
        if tool == 'hub_agora_task_add':
            origin = args.get('origin_ref')
            existing = next((row for row in state['tasks'].values() if origin and row.get('origin_ref') == origin), None)
            if existing:
                return {'ok': True, 'task': existing, 'existing': True}
            row = {**args, 'id': len(state['tasks']) + 1}
            state['tasks'][row['id']] = row
            return {'ok': True, 'task': row}
        if tool == 'hub_agora_tasks':
            return {'ok': True, 'tasks': list(state['tasks'].values())}
        if tool == 'hub_agora_task_edit':
            return {'ok': False, 'status': 409, 'error': 'The task changed'}
        raise AssertionError(tool)
    monkeypatch.setattr(tasks, 'call', call)
    return state


PROJECT = {'id': 'p', 'name': 'Silver', 'workspace': ''}


def test_one_bound_ledger_with_idempotent_preserving_import(ledger, monkeypatch):
    records = [('objective', 'OBJ-1', {'title': 'Goal', 'notes': 'Context', 'priority': 1}),
        ('requirement', 'REQ-1', {'title': 'Requirement', 'acceptance': ['Visible result'], 'status': 'accepted', 'evidence': ['old-proof']}),
        ('issue', 'SH-1', {'title': 'Bug', 'body_md': 'Original text', 'priority': 'P0', 'type': 'bug'})]
    monkeypatch.setattr(tasks, 'legacy_records', lambda project: iter(records))
    assert tasks.prepare(PROJECT)['imported'] == 3
    first = tasks.listing(PROJECT)
    assert len(first['tasks']) == 3
    requirement = first['tasks'][1]
    assert 'old-proof' in requirement['origin_snapshot']
    assert 'Visible result' in requirement['acceptance']
    assert requirement['origin_status'] == 'done'
    ledger['tasks'][1]['body'] = 'Updated by Luis'
    assert tasks.prepare(PROJECT)['imported'] == 0
    assert ledger['tasks'][1]['body'] == 'Updated by Luis'
    assert all(args.get('space_id') != 'ecosystem' for _, args in ledger['calls'])


def test_no_global_fallback_or_writes_from_read(ledger):
    ledger['spaces'] = [{'id': 'ecosystem', 'project_id': 'another'}]
    assert tasks.listing(PROJECT)['tasks'] == []
    assert [name for name, _ in ledger['calls']] == ['hub_agora_spaces']
    with pytest.raises(tasks.TaskError) as missing:
        tasks.command(PROJECT, 'task_edit', {'task_id': 1, 'priority': 0})
    assert missing.value.status == 404


def test_edit_conflict_is_preserved_and_caller_cannot_override_scope(ledger):
    with pytest.raises(tasks.TaskError) as conflict:
        tasks.command(PROJECT, 'task_edit', {'space_id': 'other', 'agent': 'luis', 'task_id': 99})
    assert conflict.value.status == 409
    args = ledger['calls'][-1][1]
    assert (args['space_id'], args['agent']) == ('silver', 'faustus')


def test_import_refuses_truncation_and_ambiguous_binding(ledger, monkeypatch):
    monkeypatch.setattr(tasks, 'legacy_records', lambda project: iter([('issue', 'x', {'title': 'Large', 'body_md': 'a' * 200001})]))
    with pytest.raises(tasks.TaskError, match='too large'):
        tasks.prepare(PROJECT)
    assert ledger['tasks'] == {}
    ledger['spaces'].append({'id': 'second', 'project_id': 'p'})
    with pytest.raises(tasks.TaskError) as conflict:
        tasks.listing(PROJECT)
    assert conflict.value.status == 409


def test_terminal_requirement_states_and_objective_priority_semantics(ledger, monkeypatch):
    records = [('requirement', status, {'title': status, 'status': status}) for status in ('rejected', 'superseded')]
    records += [('objective', str(priority), {'title': str(priority), 'priority': priority}) for priority in range(1, 5)]
    monkeypatch.setattr(tasks, 'legacy_records', lambda project: iter(records))
    tasks.prepare(PROJECT)
    rows = list(ledger['tasks'].values())
    assert [row['origin_status'] for row in rows[:2]] == ['dropped', 'dropped']
    assert [row['priority'] for row in rows[2:]] == [0, 1, 2, 3]


def test_objective_snapshot_contains_complete_evidence_and_delta_log(ledger, monkeypatch, tmp_path):
    from services import objectives
    from src.requirements import store as requirements
    from src import project_board
    project = {**PROJECT, 'workspace': str(tmp_path)}
    applied = objectives.apply_deltas(project, [{'op': 'ADD', 'title': 'Original objective', 'priority': 1}], actor='reviewer')
    oid = applied['state']['objectives'][0]['id']
    assert objectives.add_evidence(project, oid, 'test', 'EARLY-EVIDENCE', 1.0, 'Original proof')
    for index in range(60):
        objectives.append_log(project, {'id': oid, 'kind': 'evidence', 'ref': f'proof-{index}'})
    monkeypatch.setattr(requirements, 'list_requirements', lambda pid: [])
    monkeypatch.setattr(project_board, 'list_issues', lambda *a, **k: ([], None))
    tasks.prepare(project)
    import json
    snapshot = json.loads(ledger['tasks'][1]['origin_snapshot'])
    assert len(snapshot['history']) == 62
    assert snapshot['history'][0]['kind'] == 'delta'
    assert snapshot['history'][1]['ref'] == 'EARLY-EVIDENCE'
    assert snapshot['history'][-1]['ref'] == 'proof-59'
