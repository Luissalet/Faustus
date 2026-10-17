"""The native protocol replaces retracted content and preserves canonical step identity."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse
from core.models import Session
from src.group_orchestrator import NativeGroupBridge


class Sessions:
    def __init__(self):
        self.sessions={sid:Session(sid,sid,'http://fixture/v1/chat/completions','fixture-model',owner='owner') for sid in ('parent','a','b')}
        self.saves=0
    def get_session(self,sid):return self.sessions[sid]
    def save_sessions(self):self.saves+=1


def turn():
    return {'id':'turn','group_id':'parent','message':'Original instruction',
            'definition':{'executionMode':'agent','workspace':'D:/private-work',
             'participants':[{'sessionId':sid,'personaName':sid,'route':{'model':'fixture-model','endpointId':''}} for sid in ('a','b')]}}


def step():return {'id':'step','member_id':'a','text':'','detail':{}}


async def test_native_endpoint_receives_agent_mode_original_message_and_request_id(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:None)
    seen=[]
    async def endpoint(request,initial_response_timeout_s=None):
        seen.append((await request.json(),request.state.current_user,initial_response_timeout_s))
        async def stream():
            yield 'data: {"delta":"wrong intermediate"}\n\n'
            yield 'data: {"type":"response_replace","text":"Correct answer 42"}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(),headers={'X-Odysseus-Run-Id':'actual-run'})
    bridge=NativeGroupBridge(Sessions(),endpoint,FastAPI())
    progress=[]
    async def update(text,detail):progress.append((text,dict(detail)))
    t=turn();result=await bridge.invoke(turn=t,step=step(),member=t['definition']['participants'][0],owner='owner',progress=update)
    body,owner,timeout=seen[0]
    assert body['mode']=='agent' and body['message']=='Original instruction' and body['client_message_id']=='step'
    assert body['workspace']=='D:/private-work' and owner=='owner' and timeout is None
    assert result['text']=='Correct answer 42' and not result['error']
    assert progress[0][1]['run_id']=='actual-run'


async def test_finished_native_outbox_is_recovered_without_new_model_call(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:{'status':'finished','run_id':'prior','result':{'response':'Saved actual answer'}})
    calls=[]
    async def endpoint(*args,**kwargs):calls.append(True)
    bridge=NativeGroupBridge(Sessions(),endpoint,FastAPI())
    async def update(*args):pass
    t=turn();result=await bridge.invoke(turn=t,step=step(),member=t['definition']['participants'][0],owner='owner',progress=update,recovering=True)
    assert result['text']=='Saved actual answer' and calls==[]


async def test_interrupted_accepted_native_attempt_is_not_blindly_replayed(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:{'status':'running','run_id':'old-run'})
    calls=[]
    async def endpoint(*args,**kwargs):calls.append(True)
    bridge=NativeGroupBridge(Sessions(),endpoint,FastAPI())
    async def update(*args):pass
    t=turn();s=step();s['text']='partial already produced'
    result=await bridge.invoke(turn=t,step=s,member=t['definition']['participants'][0],owner='owner',progress=update,recovering=True)
    assert result['text']==s['text'] and result['detail']['outcome_unknown'] and calls==[]


async def test_native_question_is_returned_for_the_group_observer(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:None)
    async def endpoint(request,**kwargs):
        async def stream():
            yield 'data: {"type":"ask_user","data":{"question":"Which source?","question_id":"real-question"}}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream())
    bridge=NativeGroupBridge(Sessions(),endpoint,FastAPI())
    async def update(*args):pass
    t=turn();result=await bridge.invoke(turn=t,step=step(),member=t['definition']['participants'][0],owner='owner',progress=update)
    assert result['question']['question_id']=='real-question'


def test_canonical_transcript_projection_does_not_duplicate_user_or_reply():
    sessions=Sessions();bridge=NativeGroupBridge(sessions,None,FastAPI());t=turn();s={**step(),'text':'actual result'}
    bridge.user(t,'owner');bridge.user(t,'owner');bridge.reply(t,s,'owner');bridge.reply(t,s,'owner')
    assert [m.role for m in sessions.get_session('parent').history]==['user','assistant']


async def test_stop_without_owned_run_id_cannot_stop_another_use_of_the_session():
    from src import agent_runs
    bridge=NativeGroupBridge(Sessions(),None,FastAPI())
    with patch.object(agent_runs,'stop') as stop:
        await bridge.stop('a','')
        stop.assert_not_called()


def test_parallel_context_does_not_include_same_round_but_serial_does():
    from core.models import ChatMessage
    sessions=Sessions();bridge=NativeGroupBridge(sessions,None,FastAPI());t=turn()
    parent=sessions.get_session('parent')
    parent.add_message(ChatMessage('assistant','previous work',metadata={'group_step_id':'old','group_turn_id':'old-turn','group_model':'b'}))
    parent.add_message(ChatMessage('assistant','same-round work',metadata={'group_step_id':'new','group_turn_id':'turn','group_model':'b'}))
    t['definition']['mode']='parallel'
    context=bridge._context(t,t['definition']['participants'][0])
    assert 'previous work' in context and 'same-round work' not in context
    t['definition']['mode']='round-robin'
    assert 'same-round work' in bridge._context(t,t['definition']['participants'][0])

def test_saved_answer_cannot_take_reply_from_a_later_group_request():
    from core.models import ChatMessage
    sessions=Sessions();bridge=NativeGroupBridge(sessions,None,FastAPI())
    session=sessions.get_session('a')
    session.add_message(ChatMessage('system','boundary',metadata={'group_request_boundary':'step'}))
    session.add_message(ChatMessage('assistant','actual step answer'))
    session.add_message(ChatMessage('system','later boundary',metadata={'group_request_boundary':'other-step'}))
    session.add_message(ChatMessage('assistant','unrelated later answer'))
    assert bridge._saved_answer('a','step')=='actual step answer'


async def test_completed_native_tool_output_is_shared_as_evidence(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:None)
    async def endpoint(request,initial_response_timeout_s=None):
        async def stream():
            yield 'data: {"type":"tool_output","tool":"read_file","command":"evidence.txt","output":"SOURCE_VALUE=42","exit_code":0,"call_id":"real-call"}\n\n'
            yield 'data: {"delta":"Verified 42"}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream())
    sessions=Sessions();bridge=NativeGroupBridge(sessions,endpoint,FastAPI());t=turn();st=step()
    async def update(*args):pass
    result=await bridge.invoke(turn=t,step=st,member=t['definition']['participants'][0],owner='owner',progress=update)
    bridge.reply(t,{**st,**result},'owner')
    context=bridge._context(t,t['definition']['participants'][1])
    assert 'SOURCE_VALUE=42' in context and 'real-call' in context and 'read_file' in context
    assert result['detail']['tool_results'][0]['exit_code']==0


async def test_invalid_answer_keeps_actual_pending_question_available(monkeypatch):
    from src import chat_outbox
    from fastapi import HTTPException
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:None)
    async def endpoint(request,initial_response_timeout_s=None):raise HTTPException(409,'stale_revision')
    bridge=NativeGroupBridge(Sessions(),endpoint,FastAPI());t=turn();st=step()
    st['detail']={'question':{'question_id':'q','revision':1,'question':'Which file?'},'continuation':{'request_id':'answer','message':'filename'}}
    async def update(*args):pass
    result=await bridge.invoke(turn=t,step=st,member=t['definition']['participants'][0],owner='owner',progress=update)
    assert result['error']=='stale_revision' and result['question']['question_id']=='q'

def test_user_clarification_is_shared_with_next_participant():
    from core.models import ChatMessage
    sessions=Sessions();bridge=NativeGroupBridge(sessions,None,FastAPI());t=turn()
    sessions.get_session('parent').add_message(ChatMessage('user','Use evidence.txt',metadata={'group_answer_id':'actual-answer'}))
    assert 'Use evidence.txt' in bridge._context(t,t['definition']['participants'][1])


async def test_recovered_finished_transport_does_not_hide_canonical_failure(monkeypatch):
    from core.models import ChatMessage
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox,'get',lambda **kwargs:{'status':'finished','run_id':'old','result':{}})
    sessions=Sessions();session=sessions.get_session('a')
    session.add_message(ChatMessage('system','boundary',metadata={'group_request_boundary':'step'}))
    session.add_message(ChatMessage('assistant','[Agent stopped]',metadata={'failed':True,'failure':{'message':'503 service unavailable'}}))
    calls=[]
    async def endpoint(*args,**kw):calls.append(True)
    bridge=NativeGroupBridge(sessions,endpoint,FastAPI());t=turn()
    async def progress(*args):pass
    result=await bridge.invoke(turn=t,step=step(),member=t['definition']['participants'][0],owner='owner',progress=progress,recovering=True)
    assert result['error']=='503 service unavailable' and calls==[]


def test_failed_member_shares_typed_uncertainty_and_failure_with_next_model():
    sessions=Sessions();bridge=NativeGroupBridge(sessions,None,FastAPI());t=turn();st=step()
    evidence={'tool':'send','result_status':'outcome_unknown','uncertainty':{'reconcile_action':'read history'},'attempt_id':'attempt1','effect_certainty':'unknown'}
    bridge.reply(t,{**st,'status':'error','text':'Receipt missing','error':'503 after attempt','detail':{'tool_results':[evidence]}},'owner')
    context=bridge._context(t,t['definition']['participants'][1])
    assert 'outcome_unknown' in context and 'read history' in context and 'attempt1' in context
    assert 'failed: 503 after attempt' in context


async def test_reviewer_policy_and_route_survive_question_continuation(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, 'get', lambda **kwargs: None)
    seen = []
    async def endpoint(request, **kwargs):
        seen.append(await request.json())
        async def stream():
            yield 'data: {"delta":"Review complete"}\n\n'
        return StreamingResponse(stream())
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())
    t = turn()
    member = t['definition']['participants'][0]
    member.update(autonomyPreset='read_only', inputTokenBudget=180000)
    member['route'] = {'model': 'reviewer-model', 'endpointId': 'qwen-endpoint',
                       'endpointUrl': 'http://user:private@qwen/v1/chat/completions?api_key=secret'}
    st = step()
    st['detail'] = {'continuation': {'message': 'Continue review', 'autonomy_preset': 'bounded_autonomous'}}
    async def update(*args): pass
    result = await bridge.invoke(turn=t, step=st, member=member, owner='owner', progress=update)
    assert seen[0]['autonomy_preset'] == 'read_only'
    assert seen[0]['input_token_budget'] == 180000
    assert seen[0]['selected_endpoint_id'] == 'qwen-endpoint'
    assert seen[0]['model'] == 'reviewer-model'
    assert 'private' not in str(result['detail']['route']) and 'secret' not in str(result['detail']['route'])
    bridge.reply(t, {**st, **result}, 'owner')
    metadata = bridge.sessions.get_session('parent').history[-1].metadata
    assert metadata['group_route'] == result['detail']['route']


@pytest.mark.parametrize('options', [{'autonomyPreset': 'typo'}, {'inputTokenBudget': True}, {'inputTokenBudget': 1048576}])
def test_invalid_member_policy_cannot_silently_gain_write_access(options):
    from src.group_orchestrator import participant_execution_options
    with pytest.raises(ValueError):
        participant_execution_options(options)


async def test_revision_keeps_displayed_draft_but_saves_only_final_answer(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, 'get', lambda **kwargs: None)
    async def endpoint(request, **kwargs):
        async def stream():
            for frame in [
                '{"delta":"Original answer"}',
                '{"type":"response_replace","text":""}',
                '{"delta":"Discarded revision"}',
                '{"type":"response_replace","text":""}',
                '{"delta":"Final answer"}',
                '{"type":"response_replace","text":"Final answer"}',
            ]:
                yield 'data: ' + frame + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream())
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())
    progress = []
    async def update(text, detail): progress.append((text, dict(detail)))
    t = turn()
    result = await bridge.invoke(turn=t, step=step(), member=t['definition']['participants'][0], owner='owner', progress=update)
    resets = [detail for text, detail in progress if detail.get('last_event', {}).get('type') == 'response_replace' and not text]
    assert len(resets) == 2
    assert all(detail['display_text'] == 'Original answer' for detail in resets)
    assert result['text'] == 'Final answer' and 'display_text' not in result['detail']
    bridge.reply(t, {**step(), **result}, 'owner')
    assert bridge.sessions.get_session('parent').history[-1].content == 'Final answer'
