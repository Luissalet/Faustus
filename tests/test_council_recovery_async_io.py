"""Council recovery joins I/O, preserving native task and control ownership."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
from src.council.service import CouncilService


async def test_resume_queue_reads_worker_and_creates_engine_on_owner():
    owner=threading.get_ident();reads=[];scheduled=[]
    turn=SimpleNamespace(id='t',state='received');session=SimpleNamespace(id='s',status='active')
    class Store:
        def pending_sessions(self):reads.append(threading.get_ident());return [session]
        def list_turns(self,*a,**k):reads.append(threading.get_ident());return [turn]
        def get_turn(self,*a):reads.append(threading.get_ident());return turn
        def get_session(self,*a):reads.append(threading.get_ident());return session
        def claim_driver(self,*a):reads.append(threading.get_ident());return True
    svc=CouncilService(store=Store())
    def engine(value):scheduled.append(('engine',threading.get_ident()));return object()
    def schedule(*args):
        scheduled.append(('schedule',threading.get_ident()))
        svc._turn_tasks['t']=SimpleNamespace(done=lambda:False)
        return True
    svc._orchestrator_for=engine;svc._schedule_turn=schedule
    await asyncio.gather(svc.resume_pending_async(),svc.resume_pending_async())
    assert all(tid!=owner for tid in reads)
    assert sum(name=='schedule'for name,tid in scheduled)==1
    assert all(tid==owner for name,tid in scheduled)


async def test_controls_repository_worker_command_owner():
    owner=threading.get_ident();io=[];commands=[];completed=[]
    svc=None
    class Store:
        def control_sessions(self):io.append(threading.get_ident());return ['s']
        def claim_driver(self,*a):io.append(threading.get_ident());return True
        def driver(self,*a):io.append(threading.get_ident());return {'identity':svc._driver_id}
        def controls(self,*a,**k):io.append(threading.get_ident());return [{'id':'c','command':'pause','owner':'o','actor':'a','args':{}}]
        def begin_control(self,*a):io.append(threading.get_ident());return True
        def complete_control(self,*a):io.append(threading.get_ident());completed.append(a)
        def release_driver(self,*a):io.append(threading.get_ident())
    svc=CouncilService(store=Store())
    async def command(*a,**k):commands.append(threading.get_ident());return {'ok':True}
    svc.command=command
    await svc.recover_controls()
    assert all(tid!=owner for tid in io)and commands==[owner]and completed==[('c',{'ok':True})]


async def test_cancel_during_begin_joins_marks_unknown_and_releases():
    entered=threading.Event();release=threading.Event();events=[];svc=None
    class Store:
        def control_sessions(self):return ['s']
        def claim_driver(self,*a):return True
        def driver(self,*a):return {'identity':svc._driver_id}
        def controls(self,*a,**k):return [{'id':'c','command':'pause','owner':'o','actor':'a','args':{}}]
        def begin_control(self,*a):entered.set();release.wait(1);events.append('begun');return True
        def complete_control(self,key,value):events.append(('complete',value['outcome_unknown']))
        def release_driver(self,*a):events.append('released')
    svc=CouncilService(store=Store())
    async def command(*a,**k):raise AssertionError('Must not run after cancellation')
    svc.command=command;task=asyncio.create_task(svc.recover_controls())
    while not entered.is_set():await asyncio.sleep(0)
    task.cancel();await asyncio.sleep(0);assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert events==['begun',('complete',True),'released']


async def test_cancel_claim_joins_and_releases_without_scheduling():
    entered=threading.Event();release=threading.Event();events=[]
    class Store:
        def claim_driver(self,*a):entered.set();release.wait(1);events.append('claimed');return True
        def release_driver(self,*a):events.append('released')
    store=Store();svc=CouncilService(store=store)
    svc._schedule_turn=lambda *a:events.append('scheduled')
    task=asyncio.create_task(svc._start_turn_recovery(object(),'s','t',store))
    while not entered.is_set():await asyncio.sleep(0)
    task.cancel();await asyncio.sleep(0);release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert events==['claimed','released']


@pytest.mark.parametrize('state,status,prior',[('done','active',False),('received','paused',False),('received','active',True)])
async def test_settled_paused_and_predecessor_do_not_claim(state,status,prior):
    calls=[];turn=SimpleNamespace(id='t',state=state)
    class Store:
        def get_turn(self,*a):return turn
        def get_session(self,*a):return SimpleNamespace(status=status)
        def list_turns(self,*a,**k):return ([SimpleNamespace(id='earlier',state='running')]if prior else[])+[turn]
        def claim_driver(self,*a):calls.append('claim');return True
    store=Store();svc=CouncilService(store=store)
    assert not await svc._start_turn_recovery(object(),'s','t',store)
    assert calls==[]


async def test_real_command_lookup_worker_send_owner():
    owner=threading.get_ident();events=[]
    class Store:
        def driver(self,*a):events.append(('driver',threading.get_ident()));return {}
    svc=CouncilService(store=Store())
    svc._visible=lambda *a:events.append(('visible',threading.get_ident()))or SimpleNamespace(id='s',revision=0)
    svc._orchestrator_for=lambda s:events.append(('engine',threading.get_ident()))or object()
    async def send(*a,**k):events.append(('send',threading.get_ident()));return {}
    svc._send=send
    result=await svc.command('s','pause',owner='o')
    assert result['ok']is True
    assert all(tid!=owner for name,tid in events if name in ('driver','visible'))
    assert all(tid==owner for name,tid in events if name in ('engine','send'))
