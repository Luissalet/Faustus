import asyncio

from tests.test_external_events import factory, send
from core import database as cdb
from src import task_scheduler as scheduler_module


def test_opening_automations_keeps_builtin_event_occurrence(factory,monkeypatch):
    with factory() as db:
        parent=db.get(cdb.ScheduledTask,'parent')
        parent.name='Documents Tidy';parent.task_type='action';parent.action='tidy_documents'
        parent.trigger_count=1;parent.prompt='Original action configuration';db.commit()
    jid=send()['jobs'][0]['id']
    monkeypatch.setattr(cdb,'SessionLocal',factory)
    monkeypatch.setattr(scheduler_module,'SessionLocal',factory,raising=False)
    from routes import prefs_routes
    monkeypatch.setattr(prefs_routes,'_load_for_user',lambda _: {})
    scheduler=scheduler_module.TaskScheduler(None)
    asyncio.run(scheduler.ensure_defaults('tester'))
    with factory() as db:
        child=db.get(cdb.ScheduledTask,jid)
        assert child is not None
        assert child.schedule=='once' and child.status=='active'
        assert child.prompt=='Original action configuration'
