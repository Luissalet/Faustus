"""Scheduled skill-audit progress uses real action/loop and private stage fixtures."""
import asyncio
import copy
from types import SimpleNamespace

import pytest


@pytest.fixture
def audit_case(monkeypatch):
    from routes import skills_routes
    from services.memory import skills
    from src import builtin_actions, llm_core

    monkeypatch.setattr(skills_routes, '_skill_audit_jobs', {})
    monkeypatch.setattr(skills_routes, '_resolve_audit_models',
                        lambda: ('http://synthetic.invalid/v1', 'private-student', {'fixture': 'header'}, None))
    monkeypatch.setattr(llm_core, 'seconds_since_model_activity', lambda *args: None)
    records = []
    values = [{'name': 'alpha'}, {'name': 'beta'}]

    class PrivateSkills:
        def __init__(self, data_dir):
            self.data_dir = data_dir

        def load(self, owner=None):
            assert owner == 'audit-owner'
            return copy.deepcopy(values)

    monkeypatch.setattr(skills, 'SkillsManager', PrivateSkills)

    async def private_stage(manager, skill, url, model, headers, teacher, owner, log):
        records.append((skill['name'], url, model, dict(headers), teacher, owner))
        log(skill['name'] + ': testing current skill')
        return {'skill': skill['name'], 'result': 'pass'}

    monkeypatch.setattr(skills_routes, '_audit_one_skill', private_stage)
    return SimpleNamespace(routes=skills_routes, actions=builtin_actions, records=records, values=values)


def stable_job(case):
    return {key: copy.deepcopy(value) for key, value in case.routes._skill_audit_jobs[('audit-owner',)].items()
            if key not in ('started', 'finished')}


async def run_action(case, callback=None):
    return await case.actions.action_audit_skills('audit-owner', progress_cb=callback)


async def test_real_action_reports_ordered_current_stage_and_counts(audit_case):
    case = audit_case
    events = []

    def progress(message):
        assert isinstance(message, str)
        job = case.routes._skill_audit_jobs[('audit-owner',)]
        events.append((message, job['current'], job['done'], job['status']))

    result = await run_action(case, progress)
    assert result == ('Audited 2/2 unaudited skill(s): 2 pass', True)
    assert events, 'The scheduled callback was accepted but never invoked'
    assert events[0][2] == 0
    assert any('alpha' in message and current == 'alpha' and done == 0 for message, current, done, _ in events)
    assert any('alpha: testing current skill' in message for message, *_ in events)
    assert any('beta' in message and current == 'beta' and done == 1 for message, current, done, _ in events)
    assert any('beta: testing current skill' in message for message, *_ in events)
    assert [done for _, _, done, _ in events] == sorted(done for _, _, done, _ in events)
    assert events[-1][1:] == (None, 2, 'done')
    assert '2/2' in events[-1][0]
    assert all('%' not in message for message, *_ in events)
    assert [record[0] for record in case.records] == ['alpha', 'beta']


@pytest.mark.parametrize('callback_error', [RuntimeError, asyncio.CancelledError])
async def test_callback_absent_or_broken_preserves_legacy_result_and_job(audit_case, callback_error):
    case = audit_case
    baseline = await run_action(case)
    legacy_job = stable_job(case)
    legacy_calls = list(case.records)
    attempted = []

    def broken(message):
        attempted.append(message)
        raise callback_error('private callback failure')

    case.records.clear()
    result = await run_action(case, broken)
    assert result == baseline
    assert stable_job(case) == legacy_job
    assert case.records == legacy_calls
    assert attempted, 'A broken callback must still be attempted without controlling audit outcomes'


async def test_existing_skill_error_still_finishes_later_skill(audit_case, monkeypatch):
    case = audit_case
    events = []

    async def private_stage(manager, skill, *args):
        log = args[-1]
        if skill['name'] == 'alpha':
            raise ValueError('private audit stage error')
        log('beta: checking after prior failure')
        return {'skill': 'beta', 'result': 'pass'}

    monkeypatch.setattr(case.routes, '_audit_one_skill', private_stage)
    result = await run_action(case, events.append)
    assert result == ('Audited 2/2 unaudited skill(s): 1 error · 1 pass', True)
    job = stable_job(case)
    assert [(item['skill'], item['result']) for item in job['results']] == [('alpha', 'error'), ('beta', 'pass')]
    assert any('private audit stage error' in message for message in events)
    assert any('beta: checking after prior failure' in message for message in events)
    assert job['status'] == 'done' and job['done'] == 2


async def test_cancellation_retains_existing_semantics_and_reports_actual_count(audit_case, monkeypatch):
    case = audit_case
    events = []

    async def cancelled_stage(manager, skill, *args):
        case.records.append(skill['name'])
        args[-1]('alpha: existing stage before cancellation')
        raise asyncio.CancelledError()

    monkeypatch.setattr(case.routes, '_audit_one_skill', cancelled_stage)
    result = await run_action(case, events.append)
    assert result == ('Audited 0/2 unaudited skill(s): 0 results', True)
    job = stable_job(case)
    assert job['status'] == 'cancelled' and job['cancel'] is True
    assert job['done'] == 0 and job['results'] == [] and job['current'] is None
    assert case.records == ['alpha']
    assert any('(cancelled)' in message for message in events)
    assert '0/2' in events[-1]


async def test_existing_cancel_flag_stops_before_the_next_skill(audit_case, monkeypatch):
    case = audit_case
    events = []

    async def cancel_after_first(manager, skill, *args):
        case.records.append(skill['name'])
        case.routes._skill_audit_jobs[('audit-owner',)]['cancel'] = True
        return {'skill': skill['name'], 'result': 'pass'}

    monkeypatch.setattr(case.routes, '_audit_one_skill', cancel_after_first)
    result = await run_action(case, events.append)
    assert result == ('Audited 1/2 unaudited skill(s): 1 pass', True)
    assert stable_job(case)['status'] == 'cancelled'
    assert case.records == ['alpha']
    assert events and '1/2' in events[-1]


async def test_scheduler_passes_its_existing_progress_channel(audit_case):
    from src.task_scheduler import TaskScheduler
    case = audit_case
    scheduler = TaskScheduler.__new__(TaskScheduler)
    persisted = []
    scheduler._set_run_progress = lambda run_id, message: persisted.append((run_id, message))
    task = SimpleNamespace(id=None, action='audit_skills', owner='audit-owner', name='Private audit', prompt='')
    result = await scheduler._execute_action(task, run_id='private-run')
    assert result == ('Audited 2/2 unaudited skill(s): 2 pass', True)
    assert persisted and all(run_id == 'private-run' for run_id, _ in persisted)
    assert '2/2' in persisted[-1][1]


@pytest.mark.parametrize('case_name', ['no-skills', 'already-running', 'recent-model'])
async def test_noop_and_deferred_paths_do_not_start_or_report_fake_work(audit_case, monkeypatch, case_name):
    from src import llm_core
    case = audit_case
    events = []
    if case_name == 'no-skills':
        case.values.clear()
        expected = case.actions.TaskNoop
    elif case_name == 'already-running':
        case.routes._skill_audit_jobs[('audit-owner',)] = {'status': 'running'}
        expected = case.actions.TaskNoop
    else:
        monkeypatch.setattr(llm_core, 'seconds_since_model_activity', lambda *args: 1)
        expected = case.actions.TaskDeferred
    with pytest.raises(expected):
        await run_action(case, events.append)
    assert case.records == [] and events == []


async def test_missing_owner_does_not_start_audit(audit_case):
    events = []
    result = await audit_case.actions.action_audit_skills('', progress_cb=events.append)
    assert result[1] is False and 'requires an owner' in result[0]
    assert audit_case.records == [] and events == []
