from datetime import datetime
from types import SimpleNamespace
import pytest
from src.task_scheduler import compute_next_run,_resolve_task_timezone
from src.agent_loop import _classify_agent_request
from routes.task.task_routes import TaskCreate


@pytest.mark.parametrize('message',['Cada lunes busca noticias sobre fantasmas','Todos los lunes busca noticias sobre fantasmas','Every Monday find ghost news','Cada miércoles revisa estas fuentes'])
def test_recurring_request_retrieves_task_tools(message):
    result=_classify_agent_request([{'role':'user','content':message}],message)
    assert 'notes_calendar_tasks' in result['domains']


def test_weekly_keeps_madrid_wall_time_after_dst():
    before=compute_next_run('weekly','09:00',0,after=datetime(2026,10,18,10),tz_name='Europe/Madrid')
    after=compute_next_run('weekly','09:00',0,after=datetime(2026,10,25,10),tz_name='Europe/Madrid')
    assert before==datetime(2026,10,19,7)
    assert after==datetime(2026,10,26,8)


def test_explicit_timezone_does_not_need_crew():
    assert _resolve_task_timezone(None,SimpleNamespace(timezone='Europe/Madrid',crew_member_id=None))=='Europe/Madrid'
    assert _resolve_task_timezone(None,SimpleNamespace(timezone=None,crew_member_id=None)) is None


def test_rejects_unknown_timezone():
    with pytest.raises(ValueError):TaskCreate(timezone='not/a-zone')


@pytest.mark.asyncio
async def test_chat_schedules_persist_and_resume_in_their_timezone(monkeypatch, tmp_path):
    import json
    import core.database as db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.tools.system import do_manage_tasks
    engine = create_engine('sqlite:///'+str(tmp_path/'schedules.sqlite'))
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, 'SessionLocal', sessions)
    try:
        for extra in [
            {'schedule': 'weekly', 'scheduled_time': '09:00', 'day_of_week': 'lunes'},
            {'schedule': 'cron', 'cron_expression': '0 9 * * 1'},
            {'schedule': 'once', 'scheduled_date': '2099-01-05T09:00:00+01:00'},
        ]:
            result = await do_manage_tasks(json.dumps({'action': 'create', 'prompt': 'QA only', 'timezone': 'Europe/Madrid', **extra}), owner='qa')
            assert result['exit_code'] == 0, result
            task_id = result['task_id']
            with sessions() as session:
                task = session.get(db.ScheduledTask, task_id)
                assert task.owner == 'qa' and task.timezone == 'Europe/Madrid'
                if extra['schedule'] == 'once':
                    assert task.scheduled_date == datetime(2099,1,5,8)
            for action in ['pause', 'resume', 'delete']:
                result = await do_manage_tasks(json.dumps({'action': action, 'task_id': task_id}), owner='qa')
                assert result['exit_code'] == 0, result
        invalid = await do_manage_tasks(json.dumps({'action': 'create', 'prompt': 'QA', 'schedule': 'once', 'scheduled_date': '2000-01-01T09:00:00Z'}), owner='qa')
        assert invalid['exit_code'] == 1
    finally:
        engine.dispose()
