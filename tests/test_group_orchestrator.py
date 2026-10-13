"""Persistent progress, concurrent callers and observers are independent of generation."""
import asyncio
import copy
import pytest

from src.group_orchestrator import GroupCoordinator, GroupStore


def definition(group='parent',count=2,mode='round-robin'):
    return {'parentId':group,'name':'Working group','mode':mode,'executionMode':'agent','workspace':'',
            'participants':[{'sessionId':group+'-seat-'+str(i),'personaName':'Seat'+str(i),
                             'route':{'model':'model-'+str(i)}} for i in range(count)]}


class Transcript:
    def __init__(self):self.messages=[]
    def validate(self,g,o):pass
    def user(self,t,o):
        key=('user',t['id'])
        if key not in self.messages:self.messages.append(key)
    def answer(self,t,message,request_id,o):self.messages.append(("answer", request_id, message))
    def reply(self,t,s,o):
        key=('assistant',s['id'])
        if key not in self.messages:self.messages.append(key)


class Invoker:
    def __init__(self):
        self.calls=[];self.started=asyncio.Event();self.release=asyncio.Event();self.stops=[]
    async def invoke(self,**kw):
        self.calls.append((kw['turn']['message'],kw['member']['sessionId']))
        await kw['progress']('partial',{'run_id':'native-'+kw['step']['id']})
        self.started.set()
        await self.release.wait()
        return {'text':'actual '+kw['member']['route']['model'],'detail':{'run_id':'native-'+kw['step']['id']}}
    async def stop(self,sid,rid):self.stops.append((sid,rid))


async def idle(driver):
    while driver.tasks:
        await asyncio.gather(*list(driver.tasks.values()),return_exceptions=True)
        await asyncio.sleep(0)


async def test_two_submits_are_fifo_and_do_not_stop_active_work(tmp_path):
    inv=Invoker();history=Transcript();driver=GroupCoordinator(inv,history,store=GroupStore(tmp_path/'groups.db'))
    driver.adopt(definition(),'owner')
    first=await driver.submit('parent','owner','first','first-id')
    await inv.started.wait()
    duplicate=await driver.submit('parent','owner','first','first-id')
    second=await driver.submit('parent','owner','second','second-id')
    assert first['turn_id']==duplicate['turn_id'] and not duplicate['fresh']
    assert len(inv.calls)==1 and not inv.stops
    assert driver.snapshot('parent','owner')['busy']
    inv.release.set();await idle(driver)
    assert [x[0] for x in inv.calls]==['first','first','second','second']
    assert [t['status'] for t in driver.store.turns('parent')]==['done','done']
    assert len(history.messages)==6
    assert second['turn_id']!=first['turn_id']


async def test_no_observer_needed_to_advance_next_participant(tmp_path):
    inv=Invoker();driver=GroupCoordinator(inv,Transcript(),store=GroupStore(tmp_path/'groups.db'))
    driver.adopt(definition(),'owner');await driver.submit('parent','owner','work','key')
    await inv.started.wait()
    # An observer reads then disappears; it never owns the driver/task.
    assert driver.snapshot('parent','owner')['steps'][0]['text']=='partial'
    inv.release.set();await idle(driver)
    assert len(inv.calls)==2
    assert all(s['status']=='done' for s in driver.snapshot('parent','owner')['steps'])


async def test_other_coordinator_cannot_repeat_same_group(tmp_path):
    inv=Invoker();history=Transcript();path=tmp_path/'groups.db'
    a=GroupCoordinator(inv,history,store=GroupStore(path));b=GroupCoordinator(inv,history,store=GroupStore(path))
    a.adopt(definition(),'owner');await a.submit('parent','owner','work','key');await inv.started.wait()
    await b.submit('parent','owner','work','key')
    assert not b.tasks
    inv.release.set();await idle(a)
    assert len(inv.calls)==2


async def test_completed_step_survives_restart_and_is_not_invoked_again(tmp_path):
    inv=Invoker();history=Transcript();path=tmp_path/'groups.db'
    first=GroupCoordinator(inv,history,store=GroupStore(path));first.adopt(definition(),'owner')
    turn_id,_=first.store.enqueue('parent','owner','work','stable-key')
    done=first.store.steps(turn_id)[0]
    first.store.update_step(done['id'],status='done',text='Completed before restart')
    first.store.update_turn(turn_id,'running')
    second=GroupCoordinator(inv,history,store=GroupStore(path));inv.release.set()
    await second.recover();await idle(second)
    assert inv.calls==[('work','parent-seat-1')]
    assert second.store.steps(turn_id)[0]['text']=='Completed before restart'
    assert second.store.turns('parent')[0]['status']=='done'
    assert len(history.messages)==3


async def test_stop_targets_only_its_group_and_does_not_cancel_another(tmp_path):
    inv=Invoker();driver=GroupCoordinator(inv,Transcript(),store=GroupStore(tmp_path/'groups.db'))
    driver.adopt(definition('a'),'owner');driver.adopt(definition('b'),'owner')
    await driver.submit('a','owner','A','a');await driver.submit('b','owner','B','b')
    while len(inv.calls)<2:await asyncio.sleep(0)
    await driver.stop('a','owner')
    assert inv.stops and all(sid.startswith('a-') for sid,rid in inv.stops)
    inv.release.set();await idle(driver)
    assert driver.store.turns('a')[0]['status']=='stopped'
    assert driver.store.turns('b')[0]['status']=='done'


async def test_mentions_select_participant_and_more_than_eight_members_are_supported(tmp_path):
    inv=Invoker();inv.release.set();driver=GroupCoordinator(inv,Transcript(),store=GroupStore(tmp_path/'groups.db'))
    driver.adopt(definition(count=12),'owner')
    await driver.submit('parent','owner','Specific question','key',mentions=['Seat9'])
    await idle(driver)
    assert inv.calls==[('Specific question','parent-seat-9')]
    assert len(driver.store.group('parent','owner')['participants'])==12


async def test_failed_member_does_not_silently_stop_other_members(tmp_path):
    inv=Invoker();inv.release.set();original=inv.invoke
    async def invoke(**kw):
        if kw['member']['sessionId'].endswith('0'):raise ConnectionError('provider down')
        return await original(**kw)
    inv.invoke=invoke
    driver=GroupCoordinator(inv,Transcript(),store=GroupStore(tmp_path/'groups.db'));driver.adopt(definition(),'owner')
    await driver.submit('parent','owner','work','key');await idle(driver)
    assert [s['status'] for s in driver.snapshot('parent','owner')['steps']]==['error','done']
    assert driver.store.turns('parent')[0]['status']=='completed_with_errors'


def test_unknown_group_owner_cannot_read_or_change_saved_group(tmp_path):
    driver=GroupCoordinator(Invoker(),Transcript(),store=GroupStore(tmp_path/'groups.db'));driver.adopt(definition(),'owner')
    with pytest.raises(KeyError):driver.snapshot('parent','other')
    with pytest.raises(KeyError):driver.adopt(definition(),'other')


async def test_pending_question_resumes_same_member_then_fifo_queue(tmp_path):
    class Asking(Invoker):
        async def invoke(self, **kw):
            self.calls.append((kw['turn']['message'], kw['member']['sessionId'], kw['step']['detail']))
            if len(self.calls)==1:
                q={'question':'Which file?', 'question_id':'question-actual', 'revision':3}
                return {'text':'Need filename','question':q,'detail':{'question':q}}
            return {'text':'Complete','detail':kw['step']['detail']}
    inv=Asking();driver=GroupCoordinator(inv,Transcript(),store=GroupStore(tmp_path/'groups.db'))
    driver.adopt(definition(),'owner')
    first=await driver.submit('parent','owner','first','id1');await idle(driver)
    assert driver.store.turns('parent')[0]['status']=='waiting_user'
    await driver.submit('parent','owner','second','id2');await idle(driver)
    assert len(inv.calls)==1
    await driver.resume('parent','owner','evidence.txt');await idle(driver)
    assert len(inv.calls)==5
    assert inv.calls[1][1]==inv.calls[0][1]
    continuation=inv.calls[1][2]['continuation']
    assert continuation['question_id']=='question-actual' and continuation['question_revision']==3
    assert continuation['message']=='evidence.txt'
    assert [t['status'] for t in driver.store.turns('parent')]==['done','done']

async def test_recovery_reclaims_expired_lease_without_new_observer(tmp_path):
    inv=Invoker();inv.release.set();store=GroupStore(tmp_path/'groups.db')
    driver=GroupCoordinator(inv,Transcript(),store=store);driver.adopt(definition(),'owner')
    store.enqueue('parent','owner','persisted','id')
    store.claim_driver('parent','dead-process')
    with store.db() as db:db.execute("UPDATE groups SET lease_until=0")
    await driver.recover();await idle(driver)
    assert len(inv.calls)==2
    await driver.shutdown()
