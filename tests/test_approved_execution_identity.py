import json

import src.agent_loop as al
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from tests.test_agent_harness_loop import _collect, _events, _patch_common


def test_resume_distinguishes_approved_arguments_from_previous_failed_call(tmp_path, monkeypatch):
    tool = 'mcp__studio__studio_interpolate'
    content = '{"asset_id":"clip","fps":48,"wait_s":30}'
    _patch_common(monkeypatch, tool_result={'output':'{"job":{"state":"done","asset_ids":["new"]}}', 'exit_code':0})
    store = ToolApprovalStore()
    pending = store.create(owner='alice', session_id='identity', origin_run_id='origin', tool_name=tool,
                           content=content, workspace=str(tmp_path), external_untrusted_context_seen=True,
                           capabilities=capabilities_for_action(tool, content))
    grant = store.consume(pending.approval_id, decision='approve_task', owner='alice', session_id='identity', allow_continuation=False)
    assert grant.scope.value == 'single_action' and not grant.allow_remaining_actions
    seen=[]
    async def stream(_candidates, messages, **kwargs):
        seen.append(list(messages))
        yield 'data: '+json.dumps({'delta':'{"asset_id":"new","fps":48,"frames":96}'})+'\n\n'
        yield 'data: '+json.dumps({'type':'finish','finish_reason':'stop'})+'\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    msgs=[{'role':'user','content':'First try 12 fps, then recover at 48 fps. Return JSON.'},
          {'role':'assistant','content':'Allow this task to continue?', 'metadata':{'tool_events':[
           {'tool':tool,'command':'{"asset_id":"clip","fps":12}', 'output':'bad_fps: source is 24', 'exit_code':1},
           {'tool':tool,'command':content,'output':'Waiting for an exact user approval.', 'exit_code':None}]}}]
    events=_events(_collect(al.stream_agent_loop('http://x/v1','m',msgs,owner='alice',session_id='identity',
        workspace=str(tmp_path),exact_approval=grant,max_rounds=2,relevant_tools={'ask_user'},
        harness_options={'no_memory':True,'no_skills':True,'repo_map':False,'review_model':'off'})))
    receipt=next(m for m in seen[0] if str(m.get('content','')).startswith('Runtime execution receipt:'))
    facts=json.loads(receipt['content'].split('\n',1)[1])
    assert facts=={'tool':tool,'arguments':{'asset_id':'clip','fps':48,'wait_s':30},'result_status':'succeeded','exit_code':0}
    history='\n'.join(str(m.get('content','')) for m in seen[0])
    assert 'bad_fps: source is 24' in history and 'Earlier errors belong to earlier calls' in history
    counts=[json.loads(line) for message in seen[0] for line in str(message.get('content','')).splitlines()
            if line.startswith('{"tool":') and '"succeeded":' in line]
    assert any(c['arguments']=='{"asset_id":"clip","fps":48,"wait_s":30}'
               and c['succeeded']==1 and c['failed']==0 for c in counts)
    assert any(c['arguments']=='{"asset_id":"clip","fps":12}' and c['failed']==1 for c in counts)
    executed=[e for e in events if e.get('type')=='tool_output' and e.get('approved')]
    assert len(executed)==1 and json.loads(executed[0]['command'])['fps']==48


def test_lookup_schema_exposes_existing_k_contract():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema=next(s['function'] for s in FUNCTION_TOOL_SCHEMAS if s['function']['name']=='lookup_tools')
    assert schema['parameters']['properties']['k']['maximum']==12
