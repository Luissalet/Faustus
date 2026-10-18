"""Unattended MCP availability must not force every native schema."""
from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]

def route_tools(manager, explicit=(), unattended=True):
    tree=ast.parse((ROOT/'routes/chat_routes.py').read_text(encoding='utf8'))
    branch=next(node for node in ast.walk(tree)
        if isinstance(node,ast.If) and isinstance(node.test,ast.Name) and node.test.id=='unattended'
        and any(isinstance(n,ast.Name) and n.id=='_job_mcp' for n in ast.walk(node)))
    env={'unattended':unattended,'_forced_tools':set(explicit),'_catalog_tools':set(),
         'get_mcp_manager':lambda:manager,'logger':SimpleNamespace(debug=lambda *a,**k:None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[branch],type_ignores=[])),
                 'actual-unattended-route-in-RAM','exec'),env)
    return env['_forced_tools'],env['_catalog_tools']

def test_unattended_keeps_availability_separate_from_explicit_hot_tools():
    names={f'mcp__job__operation_{i}' for i in range(340)}
    manager=SimpleNamespace(get_all_tools=lambda disabled:[{'qualified_name':n,'is_disabled':False} for n in names])
    forced,catalog=route_tools(manager,{'lookup_tools','mcp__job__operation_0'})
    assert forced=={'lookup_tools','mcp__job__operation_0'}
    assert catalog==names


import asyncio
from collections import OrderedDict
import copy
import hashlib
import importlib
import json
import socket
import pytest


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    importlib.import_module('src.agent_tools')  # Register schemas before the loop import.
    import src.agent_loop as loop
    import src.tool_index as index
    import src.tool_execution as execution
    import src.tool_utils as utils
    import src.tool_security as security
    import src.tool_capabilities as capabilities
    import src.model_context as context
    from src.mcp_manager import McpManager

    def no_network(*a, **k):
        raise AssertionError('CPU tool-catalog tests must not contact any service')
    # Windows creates its local asyncio self-pipe with socket.connect. Create
    # that loop before denying service connections during the actual turn.
    runner = asyncio.Runner()
    runner.__enter__()
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    manager = McpManager()
    manager._tools['job'] = [
        {'name':f'operation_{i}', 'description':f'Isolated operation {i}',
         'input_schema':{'type':'object','properties':{'text':{'type':'string'}},'required':[]},
         'annotations':{'readOnlyHint':True,'destructiveHint':False}}
        for i in range(340)]
    manager._connections['job']={'status':'connected','name':'Isolated job','transport':'stdio'}
    executed=[]
    async def call(name, arguments, **kwargs):
        executed.append((name, copy.deepcopy(arguments)))
        return {'output':'isolated result','exit_code':0}
    monkeypatch.setattr(manager,'call_tool',call)
    monkeypatch.setattr(loop,'get_mcp_manager',lambda:manager)
    monkeypatch.setattr(utils,'get_mcp_manager',lambda:manager)
    monkeypatch.setattr(execution,'get_mcp_manager',lambda:manager)
    monkeypatch.setattr(loop,'blocked_tools_for_owner',lambda owner:set())
    monkeypatch.setattr(loop,'_agent_route_tool_mode',lambda *a,**k:(True,False,False))
    monkeypatch.setattr(security,'owner_is_admin_or_single_user',lambda owner:True)
    monkeypatch.setattr(capabilities,'tool_approval_mode',lambda:'full')
    monkeypatch.setattr(execution,'_owner_is_admin',lambda owner:True)
    monkeypatch.setattr(context,'get_context_length_known',lambda *a,**k:(1048576,True))
    monkeypatch.setattr(context,'get_context_length',lambda *a,**k:1048576)
    monkeypatch.setattr(context,'budget_context_for_model',lambda *a,**k:1048576)
    monkeypatch.setattr(loop,'_SESSION_TOOLSETS',OrderedDict())
    monkeypatch.setattr(loop,'_SESSION_TOOLSETS_LOADED',True)
    monkeypatch.setattr(loop,'_session_toolsets_path',lambda:str(tmp_path/'toolsets.json'))
    settings={'agent_tool_catalog':True,'agent_tool_schema_slim':False,
              'agent_tool_preflight':False,'agent_harness_checks':False,
              'agent_compact_mode':False,'agent_deferral_enabled':False,
              'context_engine_enabled':False,'agent_context_tools_enabled':False}
    monkeypatch.setattr(loop,'get_setting',lambda key,default=None:settings.get(key,default))
    seed={'lookup_tools','read_file','ask_user','update_plan'}
    selected=SimpleNamespace(index_mcp_tools=lambda *a,**k:None,
       get_tools_for_query=lambda *a,**k:set(seed))
    monkeypatch.setattr(index,'get_tool_index',lambda:selected)
    yield SimpleNamespace(loop=loop,manager=manager,executed=executed,settings=settings,
                          seed=seed,monkeypatch=monkeypatch,tmp=tmp_path,runner=runner)
    runner.close()


def run_turn(rt, *, message='Inspect the supplied reference.', scripts=('Finished.',),
             forced=(), catalog=True, pinned=None, disabled=(), session='catalog-test'):
    loop=rt.loop
    names={f'mcp__job__operation_{i}' for i in range(340)}
    selected_forced,selected_catalog=route_tools(rt.manager,forced)
    calls=[]
    async def provider(candidates,messages,**kwargs):
        index=len(calls)
        prepared=await kwargs['candidate_request_factory'](0,*candidates[0])
        schemas=prepared['kwargs'].get('tools') or []
        calls.append({'names':{x['function']['name'] for x in schemas},
                      'messages':copy.deepcopy(prepared['messages']),
                      'max_tokens':kwargs.get('max_tokens')})
        item=scripts[min(index,len(scripts)-1)]
        if isinstance(item,str):
            yield 'data: '+json.dumps({'delta':item})+'\n\n'
            yield 'data: '+json.dumps({'type':'finish','finish_reason':'stop'})+'\n\n'
        else:
            yield 'data: '+json.dumps({'type':'tool_calls','calls':item})+'\n\n'
            yield 'data: '+json.dumps({'type':'finish','finish_reason':'tool_calls'})+'\n\n'
        yield 'data: [DONE]\n\n'
    rt.monkeypatch.setattr(loop,'stream_llm_with_fallback',provider)
    async def drive():
        kwargs=dict(endpoint_url='http://127.0.0.1:64249/v1',model='fixture-model',headers={},
            messages=[{'role':'user','content':message}],owner='fixture-admin',
            session_id=session,max_rounds=len(scripts),max_tokens=0,context_length=1048576,
            unattended=True,forced_tools=selected_forced,relevant_tools=pinned,
            disabled_tools=set(disabled),_is_teacher_run=True,
            harness_options={'checkpoints':False,'run_tests':False,'repo_map':False,'no_skills':True,'no_memory':True})
        if catalog:kwargs['catalog_tools']=selected_catalog
        return [c async for c in loop.stream_agent_loop(**kwargs)]
    chunks=rt.runner.run(drive())
    events=[json.loads(c[6:]) for c in chunks if c.startswith('data: {')]
    assert calls, events
    return calls,events,names


def test_real_loop_defers_job_schemas_and_preserves_catalog_availability(runtime):
    calls,events,names=run_turn(runtime)
    assert names.isdisjoint(calls[0]['names'])
    assert 'lookup_tools' in calls[0]['names']
    prompt='\n'.join(str(m.get('content') or '') for m in calls[0]['messages'])
    assert 'More tools (catalog)' in prompt and 'mcp__job__operation_0' in prompt
    assert 'more' in prompt and 'lookup_tools' in prompt


def test_real_lookup_promotes_and_executes_then_stays_native(runtime):
    name='mcp__job__operation_339'
    scripts=([
        {'id':'lookup','name':'lookup_tools','arguments':json.dumps({'names':[name],'detail':'schema'})}],
        [{'id':'execute','name':name,'arguments':json.dumps({'text':'exact fixture'})}],
        'Finished.')
    calls,events,_=run_turn(runtime,scripts=scripts)
    assert name not in calls[0]['names']
    assert name in calls[1]['names'] and name in calls[2]['names']
    assert runtime.executed==[(name,{'text':'exact fixture'})]
    assert any(e.get('type')=='tool_discovery' and name in e.get('resolved',[]) for e in events)
    assert any(e.get('type')=='tool_output' and e.get('tool')==name and e.get('exit_code')==0 for e in events)


@pytest.mark.parametrize('selection',['forced','named','retrieved'])
def test_current_selected_mcp_stays_native_even_in_catalog(runtime,selection):
    name='mcp__job__operation_73'
    names={f'mcp__job__operation_{i}' for i in range(340)}
    runtime.loop._SESSION_TOOLSETS['catalog-test']=(
        frozenset(names|runtime.seed),frozenset(runtime.seed))
    kwargs={}
    if selection=='forced':kwargs['forced']={name}
    elif selection=='named':kwargs['message']='Use mcp__job__operation_73 for this reference.'
    else:
        runtime.seed.add(name)
        kwargs['message']='Inspect this reference using the job service.'
    calls,_,_=run_turn(runtime,**kwargs)
    assert name in calls[0]['names']
    assert 'mcp__job__operation_72' not in calls[0]['names']


def test_old_session_hot_catalog_does_not_return_and_cache_is_repaired(runtime):
    names={f'mcp__job__operation_{i}' for i in range(340)}
    runtime.loop._SESSION_TOOLSETS['old']=(frozenset(names|runtime.seed),frozenset(names|runtime.seed))
    calls,_,_=run_turn(runtime,session='old')
    assert names.isdisjoint(calls[0]['names'])
    assert names.isdisjoint(runtime.loop._SESSION_TOOLSETS['old'][1])
    assert names <= runtime.loop._SESSION_TOOLSETS['old'][0]
    again,_,_=run_turn(runtime,session='old')
    assert names.isdisjoint(again[0]['names'])


@pytest.mark.parametrize('mode',['disabled','lean'])
def test_catalog_disabled_and_lean_keep_existing_contracts(runtime,mode):
    if mode=='disabled':runtime.settings['agent_tool_catalog']=False
    else:runtime.monkeypatch.setattr(runtime.loop._chat_mode,'is_lean',lambda:True)
    calls,_,names=run_turn(runtime)
    if mode=='disabled':assert names <= calls[0]['names']
    else:assert names.isdisjoint(calls[0]['names'])  # Lean already disables MCP entirely.


def test_caller_pinned_scope_stays_native_and_is_not_widened(runtime):
    name='mcp__job__operation_9'
    calls,_,_=run_turn(runtime,pinned={'lookup_tools',name})
    assert name in calls[0]['names']
    assert 'mcp__job__operation_8' not in calls[0]['names']


def test_no_catalog_argument_preserves_existing_forced_contract(runtime):
    name='mcp__job__operation_6'
    calls,_,_=run_turn(runtime,catalog=False,forced={name})
    assert name in calls[0]['names']


def test_disabled_catalog_tool_stays_unoffered_and_undiscoverable(runtime):
    name='mcp__job__operation_3'
    calls,events,_=run_turn(runtime,disabled={name},scripts=([
        {'name':'lookup_tools','arguments':json.dumps({'names':[name]})}], 'Finished.'))
    assert all(name not in c['names'] for c in calls)
    assert not runtime.executed
    assert not any(name in e.get('resolved',[]) for e in events if e.get('type')=='tool_discovery')


def test_three_million_character_request_and_unbounded_output_are_preserved(runtime):
    message='Current request start. '+('reference data 0123456789\n'*128000)+' Current request end.'
    assert len(message)>3000000
    calls,_,_=run_turn(runtime,message=message)
    wanted=hashlib.sha256(message.encode()).hexdigest()
    assert any(m.get('role')=='user' and isinstance(m.get('content'),str)
       and len(m['content'])==len(message) and hashlib.sha256(m['content'].encode()).hexdigest()==wanted
       for m in calls[0]['messages'])
    assert calls[0]['max_tokens']==0


def test_weak_retrieval_keeps_existing_sticky_disclosure_rule(runtime):
    name='mcp__job__operation_73'
    names={f'mcp__job__operation_{i}' for i in range(340)}
    runtime.loop._SESSION_TOOLSETS['catalog-test']=(
        frozenset(names|runtime.seed),frozenset(runtime.seed))
    runtime.seed.add(name)
    calls,_,_=run_turn(runtime)
    assert name not in calls[0]['names']
    assert name in runtime.loop._SESSION_TOOLSETS['catalog-test'][0]


def test_chat_route_passes_separate_catalog_and_forced_arguments():
    tree=ast.parse((ROOT/'routes/chat_routes.py').read_text(encoding='utf8'))
    call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call)
        and isinstance(n.func,ast.Name) and n.func.id=='stream_agent_loop'
        and any(k.arg=='forced_tools' for k in n.keywords))
    keywords={k.arg:k.value for k in call.keywords}
    assert isinstance(keywords['forced_tools'],ast.Name) and keywords['forced_tools'].id=='_forced_tools'
    assert isinstance(keywords['catalog_tools'],ast.Name) and keywords['catalog_tools'].id=='_catalog_tools'


def test_catalog_availability_still_allows_dispatch_without_schema_promotion(runtime):
    name='mcp__job__operation_339'
    calls,events,_=run_turn(runtime,scripts=([
        {'id':'direct-catalog-call','name':name,'arguments':'{}'}], 'Finished.'))
    assert name not in calls[0]['names']
    assert runtime.executed==[(name,{})]
    assert any(e.get('type')=='tool_output' and e.get('tool')==name and e.get('exit_code')==0 for e in events)
