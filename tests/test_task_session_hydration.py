import ast
import os
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.helpers.import_state import clear_fake_database_modules
clear_fake_database_modules()
from core import database as cdb, session_manager as sessions


@pytest.mark.parametrize('with_history',[False,True])
def test_scheduler_committed_session_is_loaded_without_second_insert(tmp_path,monkeypatch,with_history):
    engine=create_engine('sqlite:///'+str(tmp_path/'sessions.db'))
    cdb.Base.metadata.create_all(engine);factory=sessionmaker(bind=engine)
    monkeypatch.setattr(sessions,'SessionLocal',factory)
    if os.environ.get('BASELINE_TASK_SESSION_REPRO')=='1':
        source=subprocess.check_output(['git','show','HEAD:core/session_manager.py'],text=True,encoding='utf-8')
        tree=ast.parse(source);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SessionManager')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='ensure_task_session')
        namespace=dict(vars(sessions));exec(compile(ast.Module(body=[fn],type_ignores=[]),'<baseline-HEAD>', 'exec'),namespace)
        monkeypatch.setattr(sessions.SessionManager,'ensure_task_session',namespace['ensure_task_session'])
    with factory() as db:
        db.add(cdb.Session(id='queued-session',name='Original task session',endpoint_url='http://example.test/v1',model='fixture',owner='tester',message_count=int(with_history)))
        if with_history:db.add(cdb.ChatMessage(id='original-message',session_id='queued-session',role='assistant',content='Prior durable result'))
        db.commit()
    manager=sessions.SessionManager()
    manager.sessions.clear()  # Scheduler commits a new row after the manager's startup cache load.
    monkeypatch.setattr(manager,'create_session',lambda *a,**kw:pytest.fail('attempted a duplicate session insert'))
    cached=manager.ensure_task_session('queued-session','Changed name','http://different.test','different',owner='tester')
    assert cached.name=='Original task session'
    assert len(cached.history)==int(with_history)
    if with_history:assert cached.history[0].content=='Prior durable result'
    with factory() as db:assert db.query(cdb.Session).count()==1
    engine.dispose()
