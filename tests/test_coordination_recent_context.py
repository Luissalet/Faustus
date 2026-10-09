"""Current scoped discussion remains readable without changing private gates."""
import asyncio,hashlib,json
from dataclasses import replace
import pytest
from src.coordination.bindings import Binding,OPERATIONS,CoordinationError,StaleBinding
from src.coordination.client import Client,compact_board
from src.coordination.store import Store
from tests.test_coordination_native import Manager

def message(mid,thread=162,body='Current coordination decision'):
    return {'id':mid,'thread_id':thread,'author':'claude','kind':'comment','body':body,
            'body_chars':len(body),'body_digest':hashlib.sha256(body.encode()).hexdigest(),'body_truncated':False}

@pytest.fixture
def native(tmp_path):
    store=Store(tmp_path)
    binding=store.save(Binding(id='bound',owner='owner',project_id='p',session_id='sid',connector_id='hub-a',actor='faustus-sid',operations=tuple(OPERATIONS),repos=('Faustus',),resources=('path:Faustus/src',),thread_ids=(32,)))
    manager=Manager();client=Client(store,binding,manager,revalidate=lambda _:None)
    task={'id':149,'repo':'Faustus','paths':['src/client.py'],'thread_id':162,'status':'review','owner':'claude','submission_revision':2}
    manager.responses['board']={'ok':True,'tasks':[task],'counts':{'review':1}}
    manager.responses['inbox']={'ok':True,'messages':[]}
    manager.responses['task']={'ok':True,'task':task,'messages':[]}
    manager.recent_schema={'type':'object','additionalProperties':False,'required':['thread_id'],'properties':{'thread_id':{'type':'integer','minimum':1},'limit':{'type':'integer','minimum':1,'maximum':200},'before_id':{'type':'integer','minimum':1}}}
    manager.responses['thread_recent']=lambda args:{'ok':True,'thread':{'id':args['thread_id']},'messages':[message(1500,args['thread_id'])],'pagination':{'has_more':False,'next_before_id':None}}
    return store,binding,manager,client

@pytest.mark.asyncio
async def test_linked_current_task_thread_is_readable_after_board_without_mutation_receipt(native):
    store,b,manager,client=native
    await client.execute('board',{})
    result=await client.execute('poll',{'thread_ids':[162]})
    assert result['recent'][0]['thread_id']==162 and result['recent'][0]['detail_available']
    assert store.cursor(b,[162])==0 and store.cursor(b)==0
    assert store.detail(b,1500,threads=[162],expected_digest=result['recent'][0]['digest'])['body']=='Current coordination decision'
    assert store.pending_count(b,[162])==1  # Excerpt is not delivery or ACK.
    with pytest.raises(CoordinationError):store.observed_task(b,149)
    with pytest.raises(CoordinationError):await client.execute('post',{'thread_id':162,'body':'No implicit posting right'})
    assert not any(name.endswith('_post') for name,_ in manager.calls)

@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'repo':'Other'},{'paths':['private/secret.py']},{'paths':{}},{'thread_id':True},{'id':True}])
async def test_outside_or_malformed_task_relationship_cannot_authorize_recent_read(native,change):
    _,_,manager,client=native
    manager.responses['board']['tasks'][0].update(change)
    result=await client.execute('poll',{'thread_ids':[162]})
    assert not result['recent'] and result['recent_unavailable']
    assert not any(name.endswith('_thread_recent') for name,_ in manager.calls)

@pytest.mark.asyncio
async def test_scope_move_during_recent_read_is_not_cached_or_delivered(native):
    store,b,manager,client=native
    def fetch(args):
        manager.responses['board']['tasks'][0]['repo']='Other'
        return {'ok':True,'thread':{'id':162},'messages':[message(1500)]}
    manager.responses['thread_recent']=fetch
    result=await client.execute('poll',{'thread_ids':[162]})
    assert not result['recent'] and 'scope changed' in result['recent_unavailable'][0]['reason']
    assert store.pending_count(b,[162])==0

@pytest.mark.asyncio
async def test_filtered_cursor_drop_and_truncated_recent_preserve_pending_coverage(native):
    store,b,manager,client=native
    manager.responses['inbox']={'ok':True,'messages':[{'id':1,'thread_id':32,'author':'luis','body':'old'}, {'id':2,'thread_id':162,'author':'luis','body':'unread'}]}
    truncated=message(1500);truncated.update(body='partial',body_chars=1000,body_truncated=True)
    manager.responses['thread_recent']={'ok':True,'thread':{'id':162},'messages':[truncated],'pagination':{'has_more':False,'next_before_id':None}}
    result=await client.execute('poll',{'thread_ids':[162]})
    assert result['cursor']==2 and store.cursor(b)==0
    assert result['recent_detail_unavailable'] and not result['recent'][0]['detail_available']
    assert store.pending_count(b,[162])==1
    reopened=Store(store.path.parent)
    assert reopened.pending(b,[162])[0]['id']==2
    assert reopened.pending_count(b,[162])==1

@pytest.mark.asyncio
async def test_board_pages_are_complete_stable_and_global_context_never_mutates(native):
    _,b,manager,client=native
    manager.responses['board']['tasks']=[{'id':i,'repo':'Other' if i>80 else 'Faustus','title':'Task'+str(i)} for i in range(1,141)]
    rows=[];first=await client.execute('board',{});page=first
    while True:
        rows.extend(page['tasks'])
        if page['next_offset'] is None:break
        page=await client.execute('board',{'offset':page['next_offset'],'digest':first['digest']})
    assert len(rows)==140 and all(row['read_only'] for row in rows[80:])
    manager.responses['board']['tasks'][0]['title']='Changed'
    with pytest.raises(CoordinationError,match='board changed'):await client.execute('board',{'offset':8,'digest':first['digest']})

@pytest.mark.asyncio
async def test_large_poll_and_task_reference_pages_stay_compact_and_review_needs_complete_details(native):
    store,b,manager,client=native
    tasks=[{**manager.responses['board']['tasks'][0],'id':n,'title':'A task title '*40} for n in range(1,101)]
    manager.responses['board']={'ok':True,'tasks':tasks,'agents':[{'id':'actor'+str(n),'doing':'x'*1000} for n in range(50)],'locks':[{'resource':'path:Faustus/src/'+str(n),'owner':'actor','task_id':n} for n in range(100)]}
    msgs=[{'id':n,'thread_id':32,'author':'luis','body':'x'*5000} for n in range(1,101)]
    manager.responses['inbox']={'ok':True,'messages':msgs,'reviews':tasks,'changes':[]}
    result=await client.execute('poll',{})
    assert len(json.dumps(result,ensure_ascii=False))<=6000
    assert result['pending_count']>=100 and result['pending_omitted']>0
    assert 'tasks' not in result['board'] and result['reviews_omitted']>0
    assert store.pending_count(b)>=100
    manager.responses['task']['messages']=msgs
    snapshot=await client.execute('task',{'task_id':149})
    receipt=snapshot['_coordination_delivery']['task_receipt'];store.confirm_task_delivery(b,receipt)
    references=snapshot['review_details'].copy();page=snapshot
    while page['next_details_offset'] is not None:
        page=await client.execute('task',{'task_id':149,'details_offset':page['next_details_offset'],'task_digest':snapshot['task_digest']})
        references.extend(page['review_details'])
    assert len(references)==100
    with pytest.raises(CoordinationError,match='complete task discussion'):store.reviewable_task(b,149)
    manager.responses['task']['task']['submission_revision']=3
    newer=await client.execute('task',{'task_id':149});store.confirm_task_delivery(b,newer['_coordination_delivery']['task_receipt'])
    with pytest.raises(CoordinationError,match='snapshot changed'):await client.execute('task',{'task_id':149,'details_offset':8,'task_digest':snapshot['task_digest']})
