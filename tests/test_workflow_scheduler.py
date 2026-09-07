"""Clock continuation, fenced late results and real SQLite lease races."""
import asyncio
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database
from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers
from src.workflows.engine import _keep_claim
from src.workflows.scheduler import WorkflowScheduler, scheduler_loop


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'scheduler.db').as_posix(),
                           connect_args={'check_same_thread': False})
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=engine))
    yield WorkflowStore()
    engine.dispose()


def create(store, nodes=None):
    definition = WorkflowDefinition.parse({
        'id': 'scheduler.test', 'version': '1.0.0', 'title': 'Local inert test',
        'nodes': nodes or [{'id': 'a', 'type': 'manual'}]})
    return store.create_run(definition)['run_id'], definition


def test_drafts_and_human_waits_never_start_from_timer(store):
    draft, _ = create(store)
    paused, definition = create(store, [{'id': 'approve', 'type': 'human_approval'}])
    store.start_node(paused, definition.nodes[0], attempt=1)
    store.finish_node(paused, 'approve', status='paused', approval_id='human-only',
                      result={'approval_id': 'human-only'})
    store.set_run_status(paused, 'paused')
    assert WorkflowScheduler(store).tick() == []
    assert store.get_run(draft)['run'].status == 'pending'
    assert store.node_runs(paused)['approve'].status == 'paused'


def test_clock_wait_continues_after_scheduler_restart(store):
    run, definition = create(store, [
        {'id': 'wait', 'type': 'wait', 'config': {'seconds': 3600}},
        {'id': 'done', 'type': 'manual', 'needs': ['wait']}])
    assert WorkflowEngine(default_handlers(), store).advance(run)['status'] == 'paused'
    assert WorkflowScheduler(store).tick() == []
    store.finish_node(run, 'wait', status='paused', result={'wake_at': '2000-01-01T00:00:00Z'})
    assert WorkflowScheduler(WorkflowStore()).tick()[0]['status'] == 'completed'
    assert store.node_runs(run)['done'].status == 'completed'
    assert store.get_run(run)['run'].reason == ''
    assert store.node_runs(run)['wait'].reason == ''
    assert WorkflowScheduler(store).tick() == []


def test_scheduler_finishes_runs_that_hit_the_per_pass_budget(store):
    run, _ = create(store, [{'id': str(i), 'type': 'manual',
                             'needs': [str(i-1)] if i else []} for i in range(3)])
    WorkflowEngine(default_handlers(), store).advance(run, max_nodes=1)
    scheduler = WorkflowScheduler(store, max_nodes=1)
    scheduler.tick()
    scheduler.tick()
    scheduler.tick()
    assert store.get_run(run)['run'].status == 'completed'


def test_old_worker_cannot_finish_or_heartbeat_reclaimed_attempt(store):
    run, definition = create(store)
    node = definition.nodes[0]
    store.start_node(run, node, attempt=1, worker_id='old')
    store.recover_expired_node_leases(now='2099-01-01T00:00:00Z')
    store.start_node(run, node, attempt=2, worker_id='new')
    assert not store.finish_node(run, node.id, status='completed', result={'wrong': True}, worker_id='old')
    assert not store.heartbeat_node(run, node.id, worker_id='old')
    assert store.finish_node(run, node.id, status='completed', result={'right': True}, worker_id='new')
    assert store.node_runs(run)[node.id].result == {'right': True}


def test_engine_rejects_late_handler_result_and_does_not_start_successor(store):
    run, _ = create(store, [{'id': 'a', 'type': 'manual'},
                            {'id': 'b', 'type': 'manual', 'needs': ['a']}])
    calls = []

    def interrupted(node, context):
        calls.append(node.id)
        store.recover_expired_node_leases(now='2099-01-01T00:00:00Z')
        return {'late': True}

    result = WorkflowEngine({'manual': interrupted}, store).advance(run)
    assert result['reason'] == 'claim_lost'
    assert calls == ['a']
    assert store.node_runs(run)['a'].status == 'pending'


def test_recovery_does_not_overwrite_heartbeat_after_its_snapshot(store, monkeypatch):
    run, definition = create(store)
    store.start_node(run, definition.nodes[0], attempt=1, worker_id='alive')
    with database.SessionLocal() as db:
        db.query(database.NodeRunRow).filter_by(workflow_run_id=run).update(
            {'lease_expires_at': '2000-01-01T00:00:00Z'})
        db.commit()
    original = store._node_types

    def heartbeat_during_snapshot(row):
        assert store.heartbeat_node(run, 'a', worker_id='alive')
        return original(row)

    monkeypatch.setattr(store, '_node_types', heartbeat_during_snapshot)
    assert store.recover_expired_node_leases() == []
    assert store.node_runs(run)['a'].status == 'running'


def test_heartbeat_is_started_and_stopped_with_handler():
    called = threading.Event()

    class Store:
        def heartbeat_node(self, *args, **kwargs):
            called.set()
            return True

    with _keep_claim(Store(), 'run', 'node', 'unique', interval=0.01):
        assert called.wait(1)
    assert not any(t.name == 'workflow-lease' and t.is_alive() for t in threading.enumerate())


def test_page_cursor_does_not_starve_newer_active_runs(store):
    runs = [create(store)[0] for _ in range(4)]
    for run in runs:
        store.set_run_status(run, 'running')
    scheduler = WorkflowScheduler(store, page_size=1)
    for _ in runs:
        scheduler.tick()
    assert all(store.get_run(run)['run'].status == 'completed' for run in runs)


def test_cancelled_run_and_stopped_worker_do_not_execute(store):
    run, _ = create(store)
    store.set_run_status(run, 'cancelled')
    assert WorkflowScheduler(store).tick() == []
    scheduler = WorkflowScheduler(store)
    scheduler.stopped.set()
    assert scheduler.tick() == []


def test_scheduler_shutdown_sets_admission_stop():
    class Worker:
        stopped = threading.Event()
        called = threading.Event()

        def tick(self):
            self.called.set()

    async def scenario():
        worker = Worker()
        task = asyncio.create_task(scheduler_loop(interval=0.01, scheduler=worker))
        for _ in range(100):
            if worker.called.is_set():
                break
            await asyncio.sleep(0.01)
        assert worker.called.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.stopped.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize('value,expected', [
    ('2026-09-07T12:30:00+02:00', True),
    ('2026-09-07T09:30:00-02:00', False),
    ('2026-09-07T11:00:00.500Z', False),
    ('2026-09-07T11:00:00', True),
    ('invalid', False),
])
def test_clock_compares_instants_not_offset_strings(value, expected):
    from src.workflows.clock import due
    assert due(value, '2026-09-07T11:00:00Z') is expected


@pytest.mark.parametrize('config', [
    {'until': 'not-a-date'}, {'seconds': float('inf')},
    {'seconds': float('nan')}, {'seconds': True}, {'seconds': 10**30},
])
def test_bad_waits_fail_with_a_reason_instead_of_parking_forever(config):
    from src.contracts import WorkflowNode
    from src.workflows.handlers import wait_handler
    result = wait_handler(WorkflowNode.parse({'id': 'wait', 'type': 'wait', 'config': config}), {})
    assert result['status'] == 'failed'
    assert result['reason']


def test_handler_cannot_park_run_on_unparseable_wake_time(store):
    run, _ = create(store)
    result = WorkflowEngine({'manual': lambda *a: {'status': 'paused', 'wake_at': 'never'}}, store).advance(run)
    assert result['status'] == 'failed'
    assert store.node_runs(run)['a'].reason == 'handler returned an invalid wake time'
