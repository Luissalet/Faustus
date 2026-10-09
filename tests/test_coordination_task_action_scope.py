"""Scope hints reuse dispatch authority without receipts, writes or reviewer identity changes."""
import copy
import json
from dataclasses import replace
import pytest
from src.coordination.authority import TASK_ACTIONS, authorize, task_action_scope
from src.coordination.bindings import Binding, OPERATIONS, READ_OPERATIONS, CoordinationError, StaleBinding
from src.coordination.client import Client, compact_board
from src.coordination.store import Store
from tests.test_coordination_native import Manager

def binding(**changes):
    return Binding(id='scope',owner='owner',project_id='project',session_id='chat',connector_id='hub-a',
                   actor='faustus-scope',operations=tuple(OPERATIONS),repos=('Example',),resources=('path:Example/src',),thread_ids=(32,),**changes)

def task(**changes):
    return {'id':7,'repo':'Example','paths':['src/a.py'],'owner':'claude','reviewer':'cursor',
            'status':'review','submission_revision':2,'thread_id':72,'latest_checkpoint':None,**changes}

def probe_args(op,metadata):
    args={'task_id':metadata['id']}
    if op=='checkpoint':args.update(expected_revision=(metadata.get('latest_checkpoint') or {}).get('revision',0),payload={})
    if op=='review':args.update(expected_submission_revision=metadata['submission_revision'],verdict='changes')
    return args

@pytest.mark.parametrize('changes',[{}, {'owner':'faustus-scope'}, {'owner':None,'submission_revision':0},
    {'status':'done'}, {'status':'dropped'}, {'repo':'Elsewhere'}, {'paths':['private/a.py']},
    {'paths':['src/a.py','src/b.py','src/c.py','src/d.py','private/e.py']},
    {'paths':['src/../private/a.py']}, {'paths':{'a':1}}, {'id':True}, {'submission_revision':True},
    {'submission_revision':0}, {'owner':'faustus-scope','latest_checkpoint':{'revision':3}}])
@pytest.mark.parametrize('read_only',[False,True])
def test_hints_match_the_dispatch_authority_for_each_task_action(changes,read_only):
    b=binding();metadata=task(**changes);original=copy.deepcopy(metadata)
    hint=task_action_scope(b,metadata,read_only=read_only)
    for op in TASK_ACTIONS:
        try:authorize(b,op,probe_args(op,metadata),task=metadata,read_only=read_only)
        except CoordinationError:assert op not in hint['allowed'],(op,hint)
        else:assert op in hint['allowed'],(op,hint)
    assert metadata==original
    assert hint['read_only']==(not hint['allowed'])
    assert {op for item in hint['unavailable'] for op in item['actions']}==set(TASK_ACTIONS)-set(hint['allowed'])

@pytest.mark.parametrize('reviewer',['claude','cursor','faustus-scope',None])
def test_assigned_reviewer_is_context_not_an_impersonated_identity(reviewer):
    metadata=task(reviewer=reviewer);hint=task_action_scope(binding(),metadata)
    assert hint['allowed']==['review']
    assert metadata['reviewer']==reviewer and metadata['owner']=='claude'
    assert hint['requires_task_delivery'] and hint['review_requires_complete_discussion_and_testing']

@pytest.mark.parametrize('metadata',[None,{}, {'id':False},task(latest_checkpoint='broken',owner='faustus-scope')])
def test_malformed_metadata_has_bounded_stable_reasons_without_crashing(metadata):
    hint=task_action_scope(binding(),metadata)
    assert len(json.dumps(hint))<1500
    assert not any('Traceback' in item['reason'] for item in hint['unavailable'])
    if metadata is None or metadata=={} or metadata=={'id':False}:assert hint['allowed']==[]
    else:assert 'checkpoint' not in hint['allowed']

def test_zero_checkpoint_and_empty_lock_set_follow_existing_contract():
    hint=task_action_scope(binding(),task(owner='faustus-scope',submission_revision=0,paths=[]))
    assert 'claim' not in hint['allowed'] and 'checkpoint' in hint['allowed'] and 'review' not in hint['allowed']
    assert authorize(binding(),'checkpoint',{'task_id':7,'expected_revision':0,'payload':{}},task=task(owner='faustus-scope',paths=[]))

def test_revoked_or_read_only_binding_has_no_task_write_eligibility():
    for b in (replace(binding(),active=False),replace(binding(),operations=tuple(READ_OPERATIONS))):
        hint=task_action_scope(b,task())
        assert not hint['allowed'] and hint['read_only']

@pytest.fixture
def native(tmp_path):
    store=Store(tmp_path);b=store.save(binding());manager=Manager()
    manager.responses={'board':{'ok':True,'tasks':[task()]},'inbox':{'ok':True,'messages':[],'reviews':[task()],
        'changes':[task(owner=b.actor,repo='Elsewhere')]},'task':{'ok':True,'task':task(),'messages':[]}}
    return store,b,manager,Client(store,b,manager,revalidate=lambda _:None)

@pytest.mark.asyncio
async def test_board_poll_hints_do_not_create_any_task_delivery_or_write_receipt(native):
    store,b,manager,client=native
    board=await client.execute('board',{});assert board['tasks'][0]['action_scope']['allowed']==['review']
    poll=await client.execute('poll',{});assert poll['reviews'][0]['action_scope']['allowed']==['review']
    assert poll['changes'][0]['action_scope']['read_only']
    assert '_coordination_delivery' not in board and '_coordination_delivery' not in poll
    with pytest.raises(CoordinationError):store.observed_task(b,7)
    with pytest.raises(CoordinationError):await client.execute('review',{'task_id':7,'expected_submission_revision':2,'verdict':'approve','body':'No implicit grant'})
    assert all(name.endswith(('_board','_inbox')) for name,_ in manager.calls)

@pytest.mark.asyncio
async def test_task_and_reference_hints_keep_original_digest_and_required_delivery(native):
    store,b,manager,client=native
    manager.responses['task']['messages']=[{'id':i,'thread_id':72,'body':'Complete discussion '+str(i),'author':'claude','kind':'comment'} for i in range(1,10)]
    result=await client.execute('task',{'task_id':7});assert result['task']['action_scope']['allowed']==['review']
    with pytest.raises(CoordinationError):store.observed_task(b,7)
    store.confirm_task_delivery(b,result['_coordination_delivery']['task_receipt'])
    observed=store.observed_task(b,7);assert 'action_scope' not in observed
    assert result['task_digest']==client.task_digest(observed,observed['_coordination_required_messages'])
    page=await client.execute('task',{'task_id':7,'details_offset':8,'task_digest':result['task_digest']})
    assert page['action_scope']==result['task']['action_scope']
    with pytest.raises(CoordinationError,match='complete task discussion'):store.reviewable_task(b,7)
    assert not any(name.endswith('_task_review') for name,_ in manager.calls)

@pytest.mark.asyncio
async def test_global_task_read_stays_read_only_even_after_explicit_task_delivery(native):
    store,b,manager,client=native;manager.responses['task']['task']=task(repo='Elsewhere')
    result=await client.execute('task',{'task_id':7});hint=result['task']['action_scope'];assert not hint['allowed'] and hint['read_only']
    store.confirm_task_delivery(b,result['_coordination_delivery']['task_receipt'])
    with pytest.raises(CoordinationError,match='repository is outside'):await client.execute('review',{'task_id':7,'expected_submission_revision':2,'verdict':'changes','body':'Scope unchanged'})
    assert not any(name.endswith('_task_review') for name,_ in manager.calls)

@pytest.mark.asyncio
async def test_read_only_turn_and_stale_binding_remain_stronger_than_hints(native):
    store,b,manager,client=native;client.read_only=True
    result=await client.execute('task',{'task_id':7});assert result['task']['action_scope']['allowed']==[]
    store.save(replace(b,active=False),expected_revision=b.revision)
    before=len(manager.calls)
    with pytest.raises(StaleBinding):await client.execute('board',{})
    assert len(manager.calls)==before

def test_complete_paths_drive_hints_even_when_preview_omits_the_outside_path():
    metadata=task(paths=['src/'+str(i) for i in range(4)]+['private/outside'])
    result=compact_board({'tasks':[metadata]},binding())
    assert result['tasks'][0]['paths_omitted']==1
    assert result['tasks'][0]['action_scope']['allowed']==[]

def test_board_scope_hints_are_bounded_and_pagination_loses_no_row():
    b=binding();tasks=[task(id=i,owner=b.actor,title='Long title '*40) for i in range(1,111)]
    board={'tasks':tasks,'agents':[{'id':'actor'+str(i),'doing':'x'*1000} for i in range(90)],
           'locks':[{'resource':'path:Example/src/'+str(i)+'x'*400,'owner':'actor','task_id':i} for i in range(80)]}
    first=compact_board(board,b);page=first;seen=[]
    while True:
        assert len(json.dumps(page,ensure_ascii=False))<=6000
        assert page['digest']==first['digest']
        seen.extend(row['id'] for row in page['tasks'])
        if page['next_offset'] is None:break
        assert page['next_offset']>page['offset']
        page=compact_board(board,b,offset=page['next_offset'])
    assert seen==list(range(1,111))
