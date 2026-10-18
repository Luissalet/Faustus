"""An armed tracker exposes its permitted tools in real requests and routes."""
import ast
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
import pytest
from src import tool_discovery, tool_security
from tests.test_unattended_tool_catalog import runtime as catalog_runtime, run_turn

ROOT=Path(__file__).resolve().parents[1]
PLAN={'plan_status','plan_task','plan_done','plan_skip','plan_next'}
HOT={'todowrite','lookup_tools'}

@lru_cache(maxsize=1)
def loop_nodes():
    tree=ast.parse((ROOT/'src/agent_loop.py').read_bytes())
    functions=[n for n in ast.walk(tree)if isinstance(n,ast.FunctionDef)and n.name in
               ('_tool_schemas_for_route','_route_schema_tools','_promote_tool_names')]
    branch=next(n for n in ast.walk(tree)if isinstance(n,ast.If)and '_relevant_tools is not None' in ast.unparse(n.test)
                and '_plan_ac_armed'in ast.unparse(n.test)and 'guide_only'in ast.unparse(n.test))
    return tree,functions,branch

@pytest.fixture
def runtime(monkeypatch):
    tree,functions,branch=loop_nodes()
    scope={'_relevant_tools':set(HOT),'_schema_tools':set(HOT),'_base_relevant_tools':set(HOT),
           '_base_schema_tools':set(HOT),'_deferred_tools':set(PLAN),'disabled_tools':set(),
           'guide_only':False,'_plan_ac_armed':True,'owner':'own','tool_policy':None,
           '_force_answer':False,'_needs_admin':False,'_ADMIN_TOOLS':set(),'_ADMIN_SCHEMA_NAMES':set(),
           '_ctx_tools_state':{'offered':True},'_csm':SimpleNamespace(CONTEXT_TOOL_NAMES=set()),
           '_filter_route_tool_schemas':lambda schemas:schemas,
           'FUNCTION_TOOL_SCHEMAS':[{'function':{'name':n}}for n in sorted(HOT|PLAN)],'_LOOKUP_TOOL':'lookup_tools'}
    scope['_route_relevant_tools']=lambda model:({'update_plan'}if model=='clamped'else set(scope['_base_relevant_tools']))
    scope['_route_finetune_modes']=lambda model:(False,model=='clamped',False,False,False)
    monkeypatch.setattr(tool_discovery,'is_known_tool',lambda name:name in HOT|PLAN)
    monkeypatch.setattr(tool_security,'owner_is_admin_or_single_user',lambda owner:True)
    exec(compile(ast.Module(body=functions,type_ignores=[]),'real-loop-tool-functions','exec'),scope)
    def arm():exec(compile(ast.Module(body=[branch],type_ignores=[]),'actual-plan-arm-branch','exec'),scope)
    def offered(*,route=False,text_only=False):
        relevant=scope['_route_relevant_tools']('other')if route else scope['_relevant_tools']
        hot=scope['_route_schema_tools']('other')if route else scope['_schema_tools']
        state={'relevant_tools':relevant,'schema_tools':hot,'mcp_schemas':[],'is_api_model':True,
               'ody_qwen_finetune_model':False,'text_only_transport':text_only}
        return {s['function']['name']for s in scope['_tool_schemas_for_route'](state)}
    return SimpleNamespace(scope=scope,arm=arm,offered=offered,tree=tree)

def test_armed_plan_offers_tools_without_lookup(runtime):
    runtime.arm()
    assert runtime.offered()==HOT|PLAN
    assert runtime.offered(route=True)==HOT|PLAN
    assert runtime.scope['_deferred_tools']==set()

@pytest.mark.parametrize('key,value',[('_plan_ac_armed',False),('guide_only',True)])
def test_unarmed_or_guide_does_not_promote(runtime,key,value):
    runtime.scope[key]=value;runtime.arm()
    assert runtime.offered()==HOT

def test_permissions_disabled_and_admin_are_checked_before_promotion(runtime,monkeypatch):
    runtime.scope['disabled_tools']={'plan_next'}
    runtime.scope['tool_policy']=SimpleNamespace(blocks=lambda name:name in {'plan_done','plan_skip'})
    monkeypatch.setattr(tool_security,'owner_is_admin_or_single_user',lambda owner:False)
    monkeypatch.setattr(tool_security,'NON_ADMIN_BLOCKED_TOOLS',{'plan_task'})
    runtime.arm()
    assert runtime.offered()==HOT|{'plan_status'}
    assert runtime.offered(route=True)==HOT|{'plan_status'}

def test_all_plan_tools_denied_does_not_promote_fallback(runtime):
    runtime.scope['tool_policy']=SimpleNamespace(blocks=lambda name:name in PLAN)
    runtime.arm();assert runtime.offered()==HOT
    assert runtime.scope['_deferred_tools']==PLAN

def test_none_selection_and_transport_boundaries(runtime):
    runtime.scope['_relevant_tools']=None;runtime.arm()
    assert runtime.scope['_schema_tools']==HOT
    runtime.scope['_relevant_tools']=set(HOT);runtime.arm()
    assert runtime.offered(text_only=True)==set()
    runtime.scope['_force_answer']=True;assert runtime.offered()==set()

def test_route_clamp_still_wins_after_plan_promotion(runtime):
    runtime.arm()
    assert runtime.scope['_route_schema_tools']('clamped')=={'update_plan'}

def test_lookup_uses_the_same_current_and_base_promotion(runtime):
    audit=tool_discovery.audit_selection('plan',sorted(PLAN),disabled_tools={'plan_skip'},fallback_pool=[])
    scope=runtime.scope;scope['_promoted']=set(audit.resolved);scope['logger']=SimpleNamespace(info=lambda *a:None)
    node=next(n for n in ast.walk(runtime.tree)if isinstance(n,ast.If)and isinstance(n.test,ast.Name)
              and n.test.id=='_promoted')
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-lookup-promotion','exec'),scope)
    assert runtime.offered()==HOT|(PLAN-{'plan_skip'})
    assert runtime.offered(route=True)==HOT|(PLAN-{'plan_skip'})


def test_real_loop_brief_and_offered_schemas_agree_for_an_armed_plan(catalog_runtime, monkeypatch):
    from src import plan_tracker, model_context, run_model_pin
    rt=catalog_runtime
    monkeypatch.setattr(model_context,'_get_context_length_cached',lambda *a,**k:(1048576,True))
    monkeypatch.setattr(run_model_pin,'_restore_wanted',lambda:False)
    monkeypatch.setattr(plan_tracker,'PLAN_TRACKER_DIR',str(rt.tmp/'plans'))
    rt.settings['agent_plan_autocontinue']=False
    message=('Implement this plan:\n'
             '## WP01: Inspect the fixture\nRead the supplied reference.\n'
             '## WP02: Implement the fixture\nApply the requested change.\n'
             '## WP03: Verify the fixture\nCheck the result.\n')
    calls,events,_=run_turn(rt,message=message,session='armed-plan')
    assert any(e.get('type')=='plan_tracker' and e.get('total')==3 for e in events)
    prompt='\n'.join(str(m.get('content') or '') for m in calls[0]['messages'])
    assert all(name in prompt for name in ('plan_status','plan_task','plan_done'))
    assert PLAN <= calls[0]['names']
    # The active tracker is durable; a later turn still obeys this turn's denylist.
    resumed,events,_=run_turn(rt,message='Continue implementing the plan.',
                            session='armed-plan',disabled={'plan_skip'})
    assert any(e.get('type')=='plan_tracker' for e in events)
    assert PLAN-{'plan_skip'} <= resumed[0]['names']
    assert 'plan_skip' not in resumed[0]['names']
