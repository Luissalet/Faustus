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
