from datetime import datetime, timezone
from types import SimpleNamespace

from src.fleet_status import epoch, build


def test_naive_task_timestamps_are_utc_not_machine_local_time():
    value=datetime(2026,10,10,12)
    assert epoch(value)==value.replace(tzinfo=timezone.utc).timestamp()
    assert epoch(None) is None


def test_native_task_cron_and_confirmed_outcomes_omit_private_fields(monkeypatch):
    from core.database import ScheduledTask, TaskRun
    import src.task_scheduler as scheduler
    created=datetime(2026,10,9,12)
    finished=datetime(2026,10,10,10)
    task=SimpleNamespace(id='qa-task',name='QA periodic task',created_at=created,status='active',trigger_type='schedule',
        schedule='cron',scheduled_time=None,scheduled_day=None,scheduled_date=None,timezone='UTC',
        cron_expression='0 */2 * * *',crew_member_id=None)
    queries=[]
    class Query:
        def __init__(self,kind): self.kind=kind
        def filter(self,*args):return self
        def group_by(self,*args):return self
        def order_by(self,*args):return self
        def limit(self,*args):return self
        def all(self): return [('qa-task',finished)] if self.kind==0 else [] if self.kind==1 else [task]
    class Database:
        def query(self,*columns):
            queries.append(columns)
            return Query(len(queries)-1)
    monkeypatch.setattr(scheduler,'get_task_policy',lambda _: {'dst_ambiguity_policy':'run_once'})
    monkeypatch.setattr(scheduler,'guarded_task_ids',lambda: set())
    result=build(Database())
    row=result['items'][0]
    assert row['last_success_ts']==epoch(finished) and row['last_status']=='ok'
    assert row['expected_slots']==[epoch(datetime(2026,10,10,12)),epoch(datetime(2026,10,10,14))]
    selected={getattr(column,'key',None) for column in queries[2]}
    assert 'prompt' not in selected and 'webhook_token' not in selected and 'endpoint_url' not in selected
    assert 'result' not in str(result) and 'tokens_used' not in str(result)


def test_native_database_ignores_running_attempt_and_keeps_guarded_cadence_unknown(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from core.database import Base, ScheduledTask, TaskRun
    import src.task_scheduler as scheduler
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(scheduler, 'get_task_policy', lambda _: {'dst_ambiguity_policy': 'run_once'})
    monkeypatch.setattr(scheduler, 'guarded_task_ids', lambda: {'guarded'})
    finished = datetime(2026, 10, 10, 10)
    with Session(engine) as db:
        for task_id in ('ordinary', 'guarded'):
            db.add(ScheduledTask(id=task_id, name='Metadata only', prompt='PRIVATE_PROMPT', schedule='cron',
                cron_expression='0 */2 * * *', timezone='UTC', status='active', created_at=datetime(2026, 10, 9)))
        db.flush()
        db.add_all([
            TaskRun(id='finished', task_id='ordinary', status='success', started_at=finished, finished_at=finished, result='PRIVATE_OUTPUT'),
            TaskRun(id='running', task_id='ordinary', status='running', started_at=datetime(2026, 10, 10, 11)),
            TaskRun(id='failed', task_id='guarded', status='error', started_at=finished, finished_at=finished, error='PRIVATE_ERROR'),
        ])
        db.commit()
        result = build(db)
    rows = {row['id']: row for row in result['items']}
    assert rows['faustus:task:ordinary']['last_success_ts'] == epoch(finished)
    assert rows['faustus:task:ordinary']['expected_slots'][1] == epoch(datetime(2026, 10, 10, 14))
    assert rows['faustus:task:guarded']['cadence_unverified'] is True
    assert rows['faustus:task:guarded']['last_error_ts'] == epoch(finished)
    assert 'PRIVATE_' not in str(result)
    engine.dispose()


def test_task_switched_back_on_after_a_long_pause_counts_its_slots_from_the_resume(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from core.database import Base, ScheduledTask, TaskRun
    import src.task_scheduler as scheduler
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(scheduler, 'get_task_policy', lambda _: {'dst_ambiguity_policy': 'run_once'})
    monkeypatch.setattr(scheduler, 'guarded_task_ids', lambda: set())
    old = datetime(2026, 9, 20, 10)
    with Session(engine) as db:
        db.add(ScheduledTask(id='slept', name='Slept', schedule='cron', cron_expression='0 */2 * * *', timezone='UTC',
                             status='paused', created_at=datetime(2026, 9, 1)))
        db.add(ScheduledTask(id='steady', name='Steady', schedule='cron', cron_expression='0 */2 * * *', timezone='UTC',
                             status='active', created_at=datetime(2026, 9, 1)))
        db.flush()
        db.add(TaskRun(id='r1', task_id='slept', status='success', started_at=old, finished_at=old))
        db.add(TaskRun(id='r2', task_id='steady', status='success', started_at=old, finished_at=old))
        db.commit()
        rows = {row['id']: row for row in build(db)['items']}
        assert rows['faustus:task:slept']['paused'] is True and rows['faustus:task:slept']['resumed_ts'] is None
        task = db.get(ScheduledTask, 'slept')
        task.status = 'active'                      # the resume, by whichever route
        db.get(ScheduledTask, 'steady').name = 'Steady, renamed'   # an edit is not a resume
        db.commit()
        rows = {row['id']: row for row in build(db)['items']}
    slept, steady = rows['faustus:task:slept'], rows['faustus:task:steady']
    assert slept['resumed_ts'] and slept['resumed_ts'] > epoch(old)
    assert slept['expected_slots'][0] > slept['resumed_ts'], 'the first slot is after the resume, not after the old success'
    assert steady['resumed_ts'] is None and steady['expected_slots'][1] < slept['resumed_ts']
    engine.dispose()


def test_resume_stamp_is_only_for_paused_to_active(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from core.database import Base, ScheduledTask
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task = ScheduledTask(id='t', name='T', schedule='daily', status='active')
        db.add(task); db.commit()
        assert task.resumed_at is None
        task.status = 'paused'; db.commit()
        assert task.resumed_at is None
        task.status = 'active'; db.commit()
        assert task.resumed_at is not None
        first = task.resumed_at
        task.status = 'active'; task.name = 'again'; db.commit()
        assert task.resumed_at == first
    engine.dispose()
