import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()
from core import database as cdb
from src import external_events as events


@pytest.fixture
def factory(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path/'events.db'), connect_args={'check_same_thread': False})
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(events, 'SessionLocal', factory)
    from src import task_scheduler
    monkeypatch.setattr(task_scheduler, 'get_task_policy', lambda _: {})
    monkeypatch.setattr(task_scheduler, 'set_task_policy', lambda *a, **k: {})
    with factory() as db:
        db.add(cdb.ScheduledTask(id='parent', owner='tester', name='Process new material', prompt='Read the event and update its document.',
            task_type='llm', trigger_type='event', trigger_event='document_created', trigger_count=1, status='active', output_target='session'))
        db.commit()
    yield factory
    engine.dispose()


def send(**kwargs):
    return events.ingest(owner='tester', source='documents', event_type='document_created', event_key='document-a',
                         payload={'document_id':'document-a'}, **kwargs)


def test_concurrent_replay_keeps_one_native_job_and_context(factory):
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: send(), range(4)))
    assert sum(not r['duplicate'] for r in receipts)==1
    with factory() as db:
        assert db.query(cdb.ExternalEvent).count()==1
        assert db.query(cdb.ExternalEventJob).count()==1
        child=db.query(cdb.ScheduledTask).filter(cdb.ScheduledTask.id!='parent').one()
        assert child.schedule=='once' and child.next_run is not None
        assert 'document-a' in child.prompt
        assert db.get(cdb.ScheduledTask,'parent').prompt=='Read the event and update its document.'
    assert all(r['jobs'][0]['task_id']==receipts[0]['jobs'][0]['task_id'] for r in receipts)


def test_two_inputs_do_not_collapse_while_first_is_pending(factory):
    send()
    events.ingest(owner='tester', source='documents', event_type='document_created',event_key='document-b',payload={'document_id':'document-b'})
    with factory() as db:
        assert db.query(cdb.ExternalEventJob).count()==2
        assert db.query(cdb.ScheduledTask).count()==3


def test_threshold_is_persisted_and_duplicates_do_not_increment(factory):
    with factory() as db:
        db.get(cdb.ScheduledTask,'parent').trigger_count=2;db.commit()
    assert send()['jobs']==[]
    assert send()['duplicate']
    second=events.ingest(owner='tester',source='documents',event_type='document_created',event_key='b',payload={})
    assert len(second['jobs'])==1
    assert len(events.list_events('tester'))==2
    assert events.list_events('someone-else')==[]


def test_same_key_different_payload_rejected(factory):
    send()
    with pytest.raises(ValueError,match='different payload'):
        events.ingest(owner='tester',source='documents',event_type='document_created',event_key='document-a',payload={'document_id':'different'})


def test_initial_mail_baseline_is_retained_without_jobs(factory):
    first=send(dispatch=False)
    assert not first['jobs']
    assert send()['duplicate'] and not send()['jobs']


def test_gmail_history_identity_ignores_redelivery_message_id():
    body={'source':'gmail_pubsub','message':{'messageId':'delivery-1','data':base64.b64encode(json.dumps({'emailAddress':'reader@example.test','historyId':'731'}).encode()).decode()}}
    one=events.decode_input(body);body['message']['messageId']='delivery-2'
    assert one==events.decode_input(body)
    assert one[1:3]==('email_received','reader@example.test:731')


def test_imap_observer_baseline_new_message_and_reconnect(factory,tmp_path,monkeypatch):
    from routes import email_routes
    monkeypatch.setattr(email_routes,'SCHEDULED_DB',str(tmp_path/'mail-observed.db'))
    with factory() as db:
        db.get(cdb.ScheduledTask,'parent').trigger_event='email_received';db.commit()
    old={'message_id':'baseline-uid'};new={'message_id':'new-uid'}
    email_routes._record_email_received_events('tester','fictional-account','INBOX',[old])
    assert events.list_events('tester')[0]['jobs']==[]
    email_routes._record_email_received_events('tester','fictional-account','INBOX',[old,new])
    email_routes._record_email_received_events('tester','fictional-account','INBOX',[old,new])
    rows=events.list_events('tester')
    assert len(rows)==2
    assert sum(len(e['jobs']) for e in rows)==1
    with factory() as db:assert db.query(cdb.ScheduledTask).count()==2


def test_http_token_webhook_old_empty_body_and_pubsub_replay(factory,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.task import task_routes
    monkeypatch.setattr(task_routes,'SessionLocal',factory)
    with factory() as db:
        db.get(cdb.ScheduledTask,'parent').webhook_token='private-hook-token';db.commit()
    class Scheduler:
        calls=0
        async def run_task_now(self,*a,**k):self.calls+=1;return True
        async def ensure_defaults(self,*a,**k):pass
    scheduler=Scheduler();app=FastAPI();app.include_router(task_routes.setup_task_routes(scheduler))
    client=TestClient(app);path='/api/tasks/parent/webhook/private-hook-token'
    assert client.post('/api/tasks/parent/webhook/wrong-token').status_code==404
    assert client.post(path).json()['ok'] and scheduler.calls==1
    body={'source':'gmail_pubsub','message':{'messageId':'delivery-1','data':base64.b64encode(json.dumps({'emailAddress':'reader@example.test','historyId':'982'}).encode()).decode()}}
    first=client.post(path,json=body).json();body['message']['messageId']='delivery-2'
    replay=client.post(path,json=body).json()
    assert first['duplicate'] is False and replay['duplicate'] is True
    assert first['jobs'][0]['task_id']==replay['jobs'][0]['task_id']
    assert scheduler.calls==1  # Event work queues durably instead of bypassing the scheduler.
    with factory() as db:
        db.add(cdb.TaskRun(id='webhook-run',task_id=first['jobs'][0]['task_id'],status='success',result='Durable event output',finished_at=cdb.utcnow_naive()));db.commit()
    assert client.get('/api/tasks/parent/runs').json()['runs'][0]['result']=='Durable event output'
    monkeypatch.setattr(task_routes,'get_current_user',lambda request:'tester')
    listed=client.get('/api/tasks').json()['tasks']
    assert len(listed)==1 and listed[0]['id']=='parent'
    assert listed[0]['run_count']==1 and listed[0]['last_run']


@pytest.mark.parametrize('first_status,expected',[(503,'failed'),(None,'unknown')])
def test_delivery_retry_keeps_successful_work_and_result(factory,monkeypatch,first_status,expected):
    jid=send(delivery={'app':'private-receiver','tool':'save_report','result_field':'content'})['jobs'][0]['id']
    with factory() as db:
        db.add(cdb.TaskRun(id='run-original',task_id=jid,status='success',result='Completed real work'))
        db.get(cdb.ScheduledTask,jid).status='completed';db.commit()
    from src import hoard_hub
    calls=[]
    def deliver(method,path,body,**kwargs):
        calls.append(body)
        return (first_status if len(calls)==1 else 200), {'ok':len(calls)>1}, {}
    monkeypatch.setattr(hoard_hub,'_call',deliver)
    asyncio.run(events.deliver_pending())
    assert events.list_events('tester')[0]['jobs'][0]['delivery']==expected
    asyncio.run(events.deliver_pending())
    assert len(calls)==1
    events.retry_delivery(jid,'tester');asyncio.run(events.deliver_pending())
    view=events.list_events('tester')[0]['jobs'][0]
    assert view['delivery']=='delivered' and view['execution']=='success' and view['run_id']=='run-original'
    assert calls[0]['arguments']==calls[1]['arguments']
    assert calls[0]['arguments']['content']=='Completed real work'
    with factory() as db:assert db.query(cdb.TaskRun).count()==1


def test_bus_delivery_references_long_result_instead_of_copying_transcript(factory,monkeypatch):
    jid=send(delivery={'event_type':'faustus.job.done'})['jobs'][0]['id']
    with factory() as db:
        db.add(cdb.TaskRun(id='long-result',task_id=jid,status='success',result='Source material '*5000));db.commit()
    from src import hoard_hub
    calls=[]
    monkeypatch.setattr(hoard_hub,'_call',lambda method,path,body,**kw:(calls.append((path,body)) or (200,{'ok':True},{})))
    asyncio.run(events.deliver_pending())
    assert len(json.dumps(calls[0][1]).encode())<16384
    assert 'result' not in calls[0][1]['data']
    assert '/api/tasks/events?task_id=parent' in calls[0][1]['data']['result_url']
    assert events.list_events('tester')[0]['jobs'][0]['result']=='Source material '*5000


def test_event_jobs_follow_current_global_service_instead_of_parent_endpoint(factory,monkeypatch):
    from src import task_scheduler
    from services import hoard_copilot
    monkeypatch.setattr(cdb,'SessionLocal',factory)
    primary={'endpoint_url':'http://global-fixture.test/v1/chat/completions','model':'first-global','headers':{}}
    monkeypatch.setattr(hoard_copilot,'global_model',lambda owner:primary)
    with factory() as db:
        parent=db.get(cdb.ScheduledTask,'parent');parent.endpoint_url='http://retired-local.test/v1';parent.model='retired-local';db.commit()
    jid=send()['jobs'][0]['id'];scheduler=task_scheduler.TaskScheduler(None);calls=[]
    async def execute(task,db):
        calls.append((task.endpoint_url,task.model));return 'Private output'
    monkeypatch.setattr(scheduler,'_execute_llm_task',execute)
    asyncio.run(scheduler._execute_task(jid,bypass_model_slot=True))
    primary['model']='second-global'
    second=events.ingest(owner='tester',source='documents',event_type='document_created',event_key='second',payload={})['jobs'][0]['id']
    asyncio.run(scheduler._execute_task(second,bypass_model_slot=True))
    assert [model for _,model in calls]==['first-global','second-global']
    assert all(url==primary['endpoint_url'] for url,_ in calls)
    with factory() as db:
        assert db.get(cdb.ScheduledTask,'parent').model=='retired-local'
        assert db.query(cdb.TaskRun).filter(cdb.TaskRun.status=='success').count()==2


def test_retry_while_receiver_is_sending_does_not_overlap_delivery(factory,monkeypatch):
    import threading
    jid=send(delivery={'app':'private-receiver','tool':'store_result'})['jobs'][0]['id']
    with factory() as db:
        db.add(cdb.TaskRun(id='one-work-run',task_id=jid,status='success',result='Retained result'));db.commit()
    from src import hoard_hub
    entered=threading.Event();release=threading.Event();calls=[]
    def receiver(*args,**kwargs):
        calls.append(args);entered.set();assert release.wait(10)
        return 200,{'ok':True},{}
    monkeypatch.setattr(hoard_hub,'_call',receiver)
    first=threading.Thread(target=events._deliver,args=(jid,));first.start()
    try:
        assert entered.wait(10)
        assert events.retry_delivery(jid,'tester')['delivery']=='sending'
        events._deliver(jid)
        assert len(calls)==1
    finally:
        release.set();first.join(10)
    assert not first.is_alive()
    view=events.list_events('tester')[0]['jobs'][0]
    assert view['delivery']=='delivered' and view['run_id']=='one-work-run'
    with factory() as db:assert db.query(cdb.TaskRun).count()==1


@pytest.mark.parametrize('decoded',[[], 'not an object', None, 17])
def test_gmail_decoded_non_object_is_a_client_error(factory,monkeypatch,decoded):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.task import task_routes
    monkeypatch.setattr(task_routes,'get_current_user',lambda request:'tester')
    app=FastAPI();app.include_router(task_routes.setup_task_routes(None))
    body={'source':'gmail_pubsub','message':{'data':base64.b64encode(json.dumps(decoded).encode()).decode()}}
    with pytest.raises(ValueError,match='must be an object'):events.decode_input(body)
    response=TestClient(app).post('/api/tasks/events',json=body)
    assert response.status_code==400 and 'must be an object' in response.json()['detail']
