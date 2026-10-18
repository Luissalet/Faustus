"""Recovery metadata waits leave the owner loop free; drivers remain on it."""
import asyncio
import threading
import pytest
from src.group_orchestrator import GroupCoordinator
from src.council import service as council
from src import shared_coordination, shared_resources


async def test_held_lazy_repository_does_not_stall_owner(monkeypatch):
    owner=threading.get_ident();entered=threading.Event();release=threading.Event();responsive=[]
    loop_progress=threading.Event();calls=[]
    class Store:
        def pending_sessions(self):
            calls.append(threading.get_ident());entered.set()
            release.wait(1)
            return []
        def control_sessions(self):calls.append(threading.get_ident());return []
        def recoverable(self):calls.append(threading.get_ident());return []
    store=Store();svc=council.CouncilService(store=store)
    monkeypatch.setattr(council,'service',lambda:svc)
    monkeypatch.setattr(shared_coordination,'_pending',False)
    monkeypatch.setattr(shared_resources,'_clients',())
    coordinator=GroupCoordinator(None,None,store=store)
    loop=asyncio.get_running_loop()
    def unblock():
        entered.wait(1);loop.call_soon_threadsafe(loop_progress.set)
        responsive.append(loop_progress.wait(.15));release.set()
    worker=threading.Thread(target=unblock);worker.start()
    try:
        await coordinator.recover()
        await asyncio.sleep(0)
        assert responsive==[True] and calls and all(tid!=owner for tid in calls)
    finally:
        release.set();worker.join();await coordinator.shutdown()


async def test_recovery_claim_competition_and_schedule_owner(monkeypatch):
    owner=threading.get_ident();claim_entered=threading.Event();release=threading.Event();claims=[];scheduled=[]
    class Store:
        def claim_driver(self,*args):claims.append(threading.get_ident());claim_entered.set();release.wait(1);return True
        def release_driver(self,*args):pass
    driver=GroupCoordinator(None,None,store=Store())
    async def drive(*args):scheduled.append(threading.get_ident());await asyncio.Event().wait()
    driver._drive=drive
    first=asyncio.create_task(driver._kick_recovery('g','o'))
    while not claim_entered.is_set():await asyncio.sleep(0)
    second=asyncio.create_task(driver._kick_recovery('g','o'))
    release.set();await asyncio.gather(first,second);await asyncio.sleep(0)
    assert len(claims)==1 and claims[0]!=owner and scheduled==[owner]
    await driver.shutdown()


async def test_cancel_claim_joins_then_releases_without_driver():
    entered=threading.Event();release=threading.Event();events=[]
    class Store:
        def claim_driver(self,*args):entered.set();release.wait(1);events.append('claimed');return True
        def release_driver(self,*args):events.append('released')
    driver=GroupCoordinator(None,None,store=Store())
    task=asyncio.create_task(driver._kick_recovery('g','o'))
    while not entered.is_set():await asyncio.sleep(0)
    task.cancel();await asyncio.sleep(0);assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert events==['claimed','released']and driver.tasks=={}


async def test_unknown_claim_is_not_replayed():
    class Unknown(RuntimeError):outcome_unknown=True
    calls=[]
    class Store:
        def claim_driver(self,*args):calls.append('claim');raise Unknown('reconcile')
        def release_driver(self,*args):calls.append('release')
    driver=GroupCoordinator(None,None,store=Store())
    with pytest.raises(Unknown):await driver._kick_recovery('g','o')
    assert calls==['claim']and not driver.tasks


async def test_binding_publication_and_resource_snapshot_stay_on_owner(monkeypatch):
    owner=threading.get_ident();events=[]
    monkeypatch.setattr(shared_coordination,'_pending',True)
    monkeypatch.setattr(shared_coordination,'_prepare_binding',lambda:events.append(('prepare',threading.get_ident()))or {'pending':True,'already_bound':True})
    monkeypatch.setattr(shared_coordination,'_publish_binding',lambda prepared,group:events.append(('publish',threading.get_ident()))or prepared)
    await shared_coordination.resume_import_async(object())
    class Client:
        holders={'worker'}
        def call(self,*args):events.append(('renew',threading.get_ident()))
    monkeypatch.setattr(shared_resources,'_clients',[Client()])
    await shared_resources.renew_async()
    assert events[0][0]=='prepare'and events[0][1]!=owner
    assert events[1]==('publish',owner)and events[2][0]=='renew'and events[2][1]!=owner


async def test_shutdown_joins_initial_scan_without_scheduling(monkeypatch):
    entered=threading.Event();release=threading.Event();events=[]
    class Store:
        def pending_sessions(self):entered.set();release.wait(1);events.append('finished_read');return []
        def control_sessions(self):events.append('controls');return []
        def recoverable(self):events.append('groups');return []
    store=Store();svc=council.CouncilService(store=store)
    monkeypatch.setattr(council,'service',lambda:svc)
    monkeypatch.setattr(shared_coordination,'_pending',False)
    monkeypatch.setattr(shared_resources,'_clients',())
    coordinator=GroupCoordinator(None,None,store=store)
    initial=asyncio.create_task(coordinator.recover())
    while not entered.is_set():await asyncio.sleep(0)
    stopping=asyncio.create_task(coordinator.shutdown());await asyncio.sleep(0)
    assert not stopping.done()
    release.set();await stopping
    with pytest.raises(asyncio.CancelledError):await initial
    assert events==['finished_read']and coordinator.tasks=={}and coordinator.control_task is None


async def test_pending_binding_keeps_canonical_authority(monkeypatch):
    from types import SimpleNamespace
    from src.council import persistence
    from src.hoard_link.coordination.rpc import RepositoryClient
    monkeypatch.setattr(shared_coordination,'_pending',True)
    monkeypatch.setattr(shared_coordination,'_bound',False)
    monkeypatch.setattr(shared_coordination,'_prepare_binding',lambda:{'pending':True,'already_bound':True})
    monkeypatch.setattr(persistence,'_STORE',None)
    group=SimpleNamespace(store='old')
    result=await shared_coordination.resume_import_async(group)
    assert result=={'ok':False,'authority':'hub','pending_import':True,'already_bound':True}
    assert isinstance(group.store,RepositoryClient)and isinstance(persistence._STORE,RepositoryClient)


async def test_cancel_binding_joins_worker_without_publishing(monkeypatch):
    entered=threading.Event();release=threading.Event();events=[]
    def prepare():entered.set();release.wait(1);events.append('prepared');return {'pending':False,'already_bound':True}
    monkeypatch.setattr(shared_coordination,'_pending',True)
    monkeypatch.setattr(shared_coordination,'_prepare_binding',prepare)
    monkeypatch.setattr(shared_coordination,'_publish_binding',lambda *a:events.append('published'))
    task=asyncio.create_task(shared_coordination.resume_import_async(object()))
    while not entered.is_set():await asyncio.sleep(0)
    task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(0)
    assert not task.done();release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert events==['prepared']and shared_coordination._pending is True
