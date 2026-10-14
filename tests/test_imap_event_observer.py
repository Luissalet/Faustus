from tests.test_external_events import factory
from core import database as cdb
from src import external_events as events
from routes import email_pollers, email_routes


def test_central_observer_baselines_and_delivers_once_after_reconnect(factory,tmp_path,monkeypatch):
    monkeypatch.setattr(cdb,'SessionLocal',factory)
    monkeypatch.setattr(email_pollers,'SCHEDULED_DB',str(tmp_path/'observed.db'))
    monkeypatch.setattr(email_routes,'SCHEDULED_DB',str(tmp_path/'observed.db'))
    with factory() as db:
        db.get(cdb.ScheduledTask,'parent').trigger_event='email_received'
        db.add(cdb.EmailAccount(id='fixture-account',owner='tester',name='Private fixture',enabled=True));db.commit()
    calls=[];state={'uids':b'8 9','validity':b'11','fail':False}
    class Connection:
        def select(self,folder,readonly):assert folder=='INBOX' and readonly;return 'OK',[]
        def response(self,key):assert key=='UIDVALIDITY';return key,[state['validity']]
        def uid(self,command,*args):
            if command=='SEARCH':return 'OK',[state['uids']]
            calls.append(args)
            if state['fail']:return 'NO',[]
            return 'OK',[(b'header',b'Message-ID: <fixture-new@example.test>\r\n\r\n')]
        def logout(self):calls.append('logout')
    def connect(account_id,owner):assert (account_id,owner)==('fixture-account','tester');return Connection()
    monkeypatch.setattr(email_pollers,'_imap_connect',connect)
    email_pollers._observe_incoming_event_mail()
    assert events.list_events('tester')==[]  # Initial UID state is not backfilled.
    state['uids']=b'10';state['fail']=True
    email_pollers._observe_incoming_event_mail();assert events.list_events('tester')==[]
    state['fail']=False
    email_pollers._observe_incoming_event_mail();email_pollers._observe_incoming_event_mail()
    rows=events.list_events('tester');assert len(rows)==1 and len(rows[0]['jobs'])==1
    email_routes._record_email_received_events('tester','fixture-account','INBOX',[{'message_id':'<fixture-new@example.test>'}])
    assert len(events.list_events('tester')[0]['jobs'])==1
    state['validity']=b'22';state['uids']=b'1 2'
    email_pollers._observe_incoming_event_mail()
    assert len(events.list_events('tester'))==1  # UIDVALIDITY reset establishes another baseline.
