import asyncio,json
from collections import namedtuple
import pytest
from src import constants,settings,tool_capabilities as caps,tool_execution as te
from src.tool_arg_policy import evaluate,evaluate_rules,override_security_decision,requires_confirmation
from src.tool_policy import ToolPolicy
Block=namedtuple('Block','tool_type content')
RULE={'id':'review-write','tool':'write_file','arg':'path','op':'prefix','value':'never-approved-prefix','action':'ask','note':'Human-defined review rule'}
@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
 data=tmp_path/'data';data.mkdir();monkeypatch.setattr(constants,'DATA_DIR',str(data));monkeypatch.setattr(settings,'SETTINGS_FILE',str(data/'settings.json'));settings._invalidate_caches()
 monkeypatch.setattr(te,'owner_is_admin_or_single_user',lambda owner:True)
 yield
 settings._invalidate_caches()
def save(mode='ask',action='ask'):
 settings.update_settings({'tool_approval_mode':mode,'tool_arg_rules':[{**RULE,'action':action}]})
@pytest.mark.parametrize('mode,required',[('ask',True),('auto',True),('full',False),('FULL',True),('invalid',True)])
def test_global_mode_only_changes_ask_enforcement_not_evaluation(mode,required):
 save(mode);decision=evaluate('write_file',{'path':'project/output.txt','tool_approval_mode':'full'})
 assert decision.action=='ask' and decision.rule_id==RULE['id']
 assert requires_confirmation(decision) is required
 allowed=caps.ToolGateDecision(allowed=True,reason=None);result=override_security_decision(allowed,decision)
 assert result.allowed is not required
 assert settings.get_setting('tool_arg_rules')==[RULE]
 assert evaluate_rules('write_file',{'path':'project/output.txt'},[RULE])==decision
@pytest.mark.parametrize('mode',['ask','auto','full','invalid'])
def test_deny_stays_deny_and_cannot_replace_an_existing_denial(mode):
 save(mode,'deny');d=evaluate('write_file',{'path':'project/output.txt'})
 assert requires_confirmation(d)
 original=caps.ToolGateDecision(allowed=False,reason='existing read-only boundary')
 assert override_security_decision(original,d) is original

def test_full_ask_does_not_remove_another_denial():
 save('full');d=evaluate('write_file',{'path':'project/output.txt'})
 original=caps.ToolGateDecision(allowed=False,reason='unavailable tool')
 assert override_security_decision(original,d) is original

def test_unavailable_mode_remains_ask(monkeypatch):
 save('full');d=evaluate('write_file',{'path':'project/output.txt'})
 def unavailable(*args):raise OSError('fixture unavailable')
 monkeypatch.setattr(caps,'get_setting',unavailable);assert requires_confirmation(d)

@pytest.mark.parametrize('mode,action,expected',[('full','ask',True),('ask','ask',False),('auto','ask',False),('full','deny',False),('invalid','ask',False)])
def test_direct_executor_matches_saved_mode_without_human_flag(tmp_path,mode,action,expected):
 save(mode,action);target=tmp_path/'output.txt'
 _,result=asyncio.run(te.execute_tool_block(Block('write_file',json.dumps({'path':str(target),'content':'written via Full','tool_approval_mode':'full'})),
  owner='qa178',workspace=str(tmp_path),security_context=caps.ToolRunSecurityContext()))
 assert target.exists() is expected
 if expected:
  assert target.read_text()=='written via Full' and result['exit_code']==0
  assert result['argument_policy_advisory']=={'rule_id':RULE['id'],'action':'ask','approval_mode':'full','requires_approval':False}
 else:assert result['exit_code']==1 and result['policy']=='tool_arg_policy'

@pytest.mark.parametrize('boundary',['disabled','read_only'])
def test_full_cannot_override_tool_availability(tmp_path,boundary):
 save('full');target=tmp_path/'output.txt'
 _,result=asyncio.run(te.execute_tool_block(Block('write_file',json.dumps({'path':str(target),'content':'must not write'})),
  owner='qa178',workspace=str(tmp_path),security_context=caps.ToolRunSecurityContext(),
  disabled_tools={'write_file'} if boundary=='disabled' else None,tool_policy=ToolPolicy(read_only=boundary=='read_only')))
 assert result['exit_code']==1 and not target.exists()

