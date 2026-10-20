"""Review budget reaches real request assembly; only transport/metadata is fake."""
import asyncio
from contextlib import asynccontextmanager
import copy
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from src import auto_review as review, engine_controls, llm_core as core


OPENAI = 'http://192.168.254.105:8000/v1'
OLLAMA = 'http://127.0.0.1:11434/api/chat'
MODEL = 'glm-5.3-flash-nvfp4'
DIFF = '+def add(left, right):\n+    return left - right\n'
REQUEST = 'Return the sum of the two arguments.'
ANSWER = json.dumps({'verdict':'issues','summary':'Wrong sign','findings':[{
    'severity':'error','file':'calc.py','line':2,'evidence':'return left - right',
    'issue':'Subtraction contradicts the requested sum.'}]})


@pytest.fixture
def transport(monkeypatch):
    from src import chat_helpers, gpu_policy, llm_trace, model_load_options, settings
    state = SimpleNamespace(requests=[],schemas=[],reply=ANSWER,status=200,finish='stop',
        failure=None,block=False,started=None,closed=False,saved={})
    monkeypatch.setattr(core,'_get_cached_response',lambda key:None)
    monkeypatch.setattr(core,'_set_cached_response',lambda *a,**k:None)
    monkeypatch.setattr(core,'_is_host_dead',lambda url:False)
    monkeypatch.setattr(core,'_clear_host_dead',lambda url:None)
    monkeypatch.setattr(core,'_schedule_heal_on_garbage',lambda *a:None)
    monkeypatch.setattr(core,'note_model_activity',lambda *a:None)
    monkeypatch.setattr(core,'_ollama_model_caps',lambda *a:None)
    monkeypatch.setattr(core,'get_context_length',lambda *a:32768)
    monkeypatch.setattr(chat_helpers,'_llamacpp_effort_cache',{
        ('192.168.254.105',8000):(('low','high','max'),time.time()+3600)})
    monkeypatch.setattr(gpu_policy,'preferred_main_gpu',lambda *a:None)
    monkeypatch.setattr(llm_trace,'record_call',lambda **k:None)
    monkeypatch.setattr(settings,'load_settings',lambda:{'sparks_endpoints':{'own-recipe':'own-endpoint'}})
    monkeypatch.setattr(settings,'get_setting',lambda key,default=None:default)
    monkeypatch.setattr(core,'_endpoint_id_for_url',lambda url:'own-endpoint' if url.startswith(OPENAI) else None)
    # Real saved-option resolution/sanitization, with only the stored metadata
    # and endpoint catalog supplied by this private fixture.
    monkeypatch.setattr(model_load_options,'all_options',lambda:state.saved)
    monkeypatch.setattr(model_load_options,'_endpoint_bases',lambda:{'own-ollama':OLLAMA})
    monkeypatch.setattr(engine_controls,'_cache',{})
    monkeypatch.setattr(engine_controls,'_schema_cache',{})
    monkeypatch.setattr(engine_controls,'_direct_history_cache',{})
    document = {'paths':{'/v1/chat/completions':{'post':{'requestBody':{'content':{
        'application/json':{'schema':{'$ref':'#/components/schemas/ChatCompletionRequest'}}}}}}},
        'components':{'schemas':{'ChatCompletionRequest':{'type':'object','properties':{
            'thinking_token_budget':{'anyOf':[{'type':'integer'},{'type':'null'}]},
            'chat_template_kwargs':{'anyOf':[{'type':'object','additionalProperties':True},{'type':'null'}]}}}}}}
    class Opener:
        def open(self,url,timeout):
            assert url == OPENAI[:-3]+'/openapi.json'
            state.schemas.append(url)
            return io.BytesIO(json.dumps(document).encode())
    monkeypatch.setattr(engine_controls.urllib.request,'build_opener',lambda *a:Opener())
    @asynccontextmanager
    async def admission(*a,**k):
        yield
    monkeypatch.setattr(core,'_local_model_slot',admission)
    async def post(request):
        assert request.method == 'POST'
        payload = json.loads(request.content)
        state.requests.append(dict(url=str(request.url),payload=payload,timeout=copy.deepcopy(request.extensions.get('timeout'))))
        if state.started is not None: state.started.set()
        if state.block: await asyncio.Event().wait()
        if state.failure is not None: raise state.failure
        if state.status != 200:
            return httpx.Response(state.status,json={'error':{'message':'own synthetic upstream error'}})
        if request.url.path == '/api/chat':
            body = dict(model=payload['model'],message={'content':state.reply},done=True,eval_count=21,prompt_eval_count=34)
        else:
            body = dict(id='own-response',model=payload['model'],usage={
                'prompt_tokens':8974,'completion_tokens':1200,
                'completion_tokens_details':{'reasoning_tokens':1200}},choices=[{
                    'index':0,'finish_reason':state.finish,'message':{'content':state.reply,'reasoning':None}}])
        return httpx.Response(200,json=body)
    async def client_call(function):
        client = httpx.AsyncClient(transport=httpx.MockTransport(post),trust_env=False)
        try:
            async with client:
                monkeypatch.setattr(core,'_get_http_client',lambda:client)
                return await function()
        finally:
            state.closed = client.is_closed
    async def invoke(url=OPENAI,model=MODEL):
        return await client_call(lambda:review._call_reviewer(diff=DIFF,files=['calc.py'],user_text=REQUEST,
            endpoint_url=url,reviewer=model,headers={'Authorization':'Bearer own-fixture'},tests=None,timeout=1,workload='foreground'))
    state.invoke,state.client_call = invoke,client_call
    return state


@pytest.mark.parametrize('url,model',[(OPENAI,MODEL),('https://output-budget.invalid/v1','o3-mini'),(OLLAMA,'fixture-local')])
def test_reviewer_wire_uses_normal_service_output_budget(transport,url,model):
    parsed,error = asyncio.run(transport.invoke(url,model))
    assert error is None and parsed['verdict']=='issues'
    assert parsed['findings'][0]['grounded'] is True
    assert len(transport.requests)==1 and transport.closed
    wire = transport.requests[0]['payload']
    assert wire['model']==model
    assert 'max_tokens' not in wire and 'max_completion_tokens' not in wire
    assert 'num_predict' not in wire.get('options',{})
    assert parsed['response_evidence'][0]['http_status']==200
    if url==OLLAMA:
        assert wire['format']==review.REVIEW_SCHEMA
    if url==OPENAI:
        assert wire['chat_template_kwargs']['enable_thinking'] is True
        assert wire['chat_template_kwargs']['clear_thinking'] is False
        assert 'thinking_token_budget' not in wire


def test_explicit_saved_ollama_budget_is_not_replaced(transport):
    transport.saved = {'own-ollama|fixture-local':{'extra':{'num_predict':7777,'top_p':0.72},'keep_alive':'10m'}}
    parsed,error = asyncio.run(transport.invoke(OLLAMA,'fixture-local'))
    assert error is None and parsed['findings'][0]['grounded']
    wire = transport.requests[0]['payload']
    assert wire['options']['num_predict']==7777 and wire['options']['top_p']==0.72
    assert wire['keep_alive']=='10m' and len(transport.requests)==1


@pytest.mark.parametrize('budget',[4096,-1])
def test_normal_zero_budget_keeps_explicit_reasoning_controls(transport,budget):
    observed = []
    async def run():
        return await transport.client_call(lambda:core.llm_call_async(OPENAI,MODEL,[{'role':'user','content':'own fixture'}],
            max_tokens=0,max_retries=1,gen_overrides={'think':True,'reasoning_effort':'high','reasoning_budget':budget},
            _response_observer=observed.append))
    assert asyncio.run(run())==ANSWER
    wire = transport.requests[0]['payload']
    assert wire['reasoning_effort']=='high' and wire['thinking_token_budget']==budget
    assert wire['chat_template_kwargs']['enable_thinking'] is True
    assert 'max_tokens' not in wire and 'max_completion_tokens' not in wire
    assert observed[0]['request']['controls']['thinking_token_budget']==budget


def test_empty_length_remains_error_with_actual_response_evidence_and_no_replay(transport):
    transport.reply,transport.finish = None,'length'
    parsed,error = asyncio.run(transport.invoke())
    assert error and parsed['verdict']=='error' and parsed['findings']==[]
    assert len(transport.requests)==1 and transport.closed
    evidence = parsed['response_evidence'][0]
    assert evidence['response']['choices'][0]['finish_reason']=='length'
    assert evidence['response']['choices'][0]['channels']['content']=={'present':True,'type':'NoneType'}
    assert evidence['response']['usage']['reasoning_tokens']==1200


def test_unparsed_reply_does_not_become_a_clean_review_or_publish_think_block(transport):
    transport.reply = '<think>PRIVATE INTERNAL THOUGHT</think>not a review'
    parsed,error = asyncio.run(transport.invoke())
    assert error and parsed['verdict']=='unparsed' and parsed['findings']==[]
    assert 'PRIVATE INTERNAL THOUGHT' not in json.dumps(parsed)
    assert len(transport.requests)==1 and parsed['response_evidence'][0]['http_status']==200


@pytest.mark.parametrize('status',[400,503])
def test_http_failure_has_one_attempt_and_keeps_observer(transport,status):
    transport.status = status
    parsed,error = asyncio.run(transport.invoke())
    assert error and parsed['verdict']=='error'
    assert len(transport.requests)==1 and transport.closed
    assert parsed['response_evidence'][0]['http_status']==status


def test_transport_timeout_does_not_replay_or_invent_a_response(transport):
    transport.failure = httpx.ReadTimeout('own timeout')
    parsed,error = asyncio.run(transport.invoke())
    assert error and parsed['verdict']=='error' and len(transport.requests)==1
    assert not parsed.get('response_evidence') and transport.closed


def test_outer_deadline_is_preserved_and_closes_pending_transport(transport,monkeypatch):
    transport.block = True
    wait = asyncio.wait_for
    limits = []
    def scaled(pending,timeout):
        limits.append(timeout)
        return wait(pending,timeout=0.03)
    monkeypatch.setattr(review.asyncio,'wait_for',scaled)
    parsed,error = asyncio.run(transport.invoke())
    assert limits==[31] and 'timed out' in error
    assert parsed['verdict']=='error' and len(transport.requests)==1 and transport.closed


def test_caller_cancel_propagates_and_leaves_no_worker(transport):
    async def run():
        transport.block = True
        transport.started = asyncio.Event()
        task = asyncio.create_task(transport.invoke())
        await asyncio.wait_for(transport.started.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await asyncio.sleep(0)
        assert asyncio.all_tasks()=={asyncio.current_task()}
        assert len(transport.requests)==1 and transport.closed
    asyncio.run(run())


def test_each_real_group_uses_normal_budget_without_extra_attempts(transport,monkeypatch):
    transport.reply = '{"verdict":"ok","summary":"own fixture","findings":[]}'
    monkeypatch.setattr(review,'_setting',lambda key,default:1 if key=='auto_review_group_threshold_files' else default)
    monkeypatch.setattr(review,'turn_diff',lambda *a,**k:{'diff':DIFF,'source':'checkpoint'})
    monkeypatch.setattr(review,'per_file_diffs',lambda *a,**k:{'a.py':DIFF,'b.py':DIFF})
    async def groups(*a,**k): return [['a.py'],['b.py']]
    monkeypatch.setattr(review,'group_files',groups)
    async def run():
        return await transport.client_call(lambda:review.review_turn(workspace='own-fixture',changed=['a.py','b.py'],
            checkpoint_sha='own',user_text=REQUEST,endpoint_url=OPENAI,model=MODEL))
    result = asyncio.run(run())
    assert result['verdict']=='ok' and result['groups']==2 and result['coverage_complete']
    assert len(transport.requests)==2 and len(result['response_evidence'])==2
    assert all('max_tokens' not in row['payload'] and 'max_completion_tokens' not in row['payload'] for row in transport.requests)
