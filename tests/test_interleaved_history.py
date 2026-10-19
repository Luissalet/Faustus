"""Native interleaved history uses served identity and cached capabilities."""
from copy import deepcopy
import pytest
from src import engine_controls as ec
from src.agent_loop import _append_tool_results, _history_for_candidate
from src.history_projection import project
import asyncio,json,os,time
from contextlib import asynccontextmanager,contextmanager
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread

MODEL='fixture/GLM-5.3-Flash-NVFP4'
BASE='http://127.0.0.1:39159/v1'

def document():
    return {'paths':{'/v1/chat/completions':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/ChatCompletionRequest'}}}}}}},
      'components':{'schemas':{'ChatCompletionRequest':{'type':'object','properties':{
        'thinking_token_budget':{'anyOf':[{'type':'integer'},{'type':'null'}]},
        'chat_template_kwargs':{'anyOf':[{'type':'object','additionalProperties':True},{'type':'null'}]}}}}}}

@pytest.fixture(autouse=True)
def clean_capabilities(monkeypatch):
    ec._schema_cache.clear();ec._cache.clear()
    from src import tool_index
    monkeypatch.setattr(tool_index,'get_tool_index',lambda:None)
    yield
    ec._schema_cache.clear();ec._cache.clear()

def refresh(base=BASE,model=MODEL,doc=None):
    ec.refresh_json_schema([{'endpoint_id':'managed-test','base_url':base,'models':[model]}],fetch_openapi=lambda _:document() if doc is None else doc)

def round_message(messages,n,base=BASE,model=MODEL):
    _append_tool_results(messages,'',[{'id':f'c{n}','name':'echo','arguments':'{}'}],['ok'],['ok'],True,n,
                         round_reasoning=f'ROUND{n}_SENTINEL',model=model,endpoint_url=base,tool_images_enabled=False)

def thoughts(messages):return [m.get('reasoning_content') for m in messages if m.get('role')=='assistant']

def test_verified_native_rounds_survive_real_producer_and_projection():
    refresh();messages=[{'role':'user','content':'current user'}]
    round_message(messages,1);round_message(messages,2)
    assert thoughts(messages)==['ROUND1_SENTINEL','ROUND2_SENTINEL']
    assert thoughts(project(messages,'openai_chat',record=False).messages)==['ROUND1_SENTINEL','ROUND2_SENTINEL']

def test_unverified_and_other_models_keep_previous_behavior():
    refresh();messages=[{'role':'user','content':'current user'}]
    round_message(messages,1,model='another-model');round_message(messages,2,model='another-model')
    assert thoughts(messages)==[None,'ROUND2_SENTINEL']

def test_qwen_behavior_remains_unchanged():
    messages=[{'role':'user','content':'current user'}]
    round_message(messages,1,model='qwen3.8');round_message(messages,2,model='qwen3.8')
    assert thoughts(messages)==['ROUND1_SENTINEL','ROUND2_SENTINEL']

def test_capability_is_identity_scoped_cache_only(monkeypatch):
    refresh()
    monkeypatch.setattr(ec.urllib.request,'build_opener',lambda *a:pytest.fail('generation must not probe'))
    assert ec.interleaved_history_capability(BASE,MODEL)['interleaved_history'] is True
    assert not ec.interleaved_history_capability(BASE,'another-model')['interleaved_history']
    assert not ec.interleaved_history_capability('http://127.0.0.1:39160/v1',MODEL)['interleaved_history']
    ec.refresh_json_schema([],fetch_openapi=lambda _:pytest.fail('retiring identity needs no network'))
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']

@pytest.mark.parametrize('field,bad',[('thinking_token_budget',{'type':'string'}),('chat_template_kwargs',{'type':'string'})])
def test_malformed_native_schema_cannot_enable_history(field,bad):
    doc=deepcopy(document());doc['components']['schemas']['ChatCompletionRequest']['properties'][field]=bad
    refresh(doc=doc)
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']

def test_fallback_drops_other_route_reasoning_without_mutating_source():
    refresh();messages=[{'role':'user','content':'current user'}]
    round_message(messages,1);round_message(messages,2);before=deepcopy(messages)
    same=_history_for_candidate(messages,BASE+'/chat/completions',MODEL)
    assert thoughts(same)==['ROUND1_SENTINEL','ROUND2_SENTINEL']
    for base,model in [(BASE,'another-model'),('http://127.0.0.1:39160/v1',MODEL)]:
        sent=_history_for_candidate(messages,base,model)
        assert thoughts(sent)==[None,None]
        assert [m.get('tool_call_id') for m in sent if m['role']=='tool']==['c1','c2']
    assert messages==before
    projected=project(same,'openai_chat',record=False).messages
    assert all('_reasoning_origin' not in m for m in projected)

def test_cache_expiry_changed_model_and_retirement_do_not_share_capability(monkeypatch):
    refresh();stamp=next(iter(ec._schema_cache.values()))['at']
    monkeypatch.setattr(ec.time,'monotonic',lambda:stamp+61)
    assert ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']
    assert not ec.json_schema_capability(BASE,'managed-test')['json_schema']
    refresh(model='another-model')
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']
    refresh()
    assert ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']


@pytest.mark.parametrize('refresh_state',['expired','in_flight','unavailable'])
def test_metadata_refresh_cannot_erase_current_turn_reasoning(monkeypatch,refresh_state):
    from threading import Event
    refresh();stamp=next(iter(ec._schema_cache.values()))['at']
    messages=[{'role':'user','content':'same user'}]
    round_message(messages,1)
    monkeypatch.setattr(ec.time,'monotonic',lambda:stamp+61)
    entered,release=Event(),Event()
    thread=None
    if refresh_state=='in_flight':
        def blocked(_):
            entered.set()
            assert release.wait(3)
            return document()
        thread=Thread(target=lambda:ec.refresh_json_schema(
            [{'endpoint_id':'managed-test','base_url':BASE,'models':[MODEL]}],fetch_openapi=blocked))
        thread.start()
        assert entered.wait(3)
    elif refresh_state=='unavailable':
        def unavailable(_):raise OSError('metadata unavailable')
        ec.refresh_json_schema([{'endpoint_id':'managed-test','base_url':BASE,'models':[MODEL]}],fetch_openapi=unavailable)
    try:
        round_message(messages,2)
        assert thoughts(messages)==['ROUND1_SENTINEL','ROUND2_SENTINEL']
    finally:
        release.set()
        if thread:
            thread.join(3)
            assert not thread.is_alive()
    monkeypatch.setattr(ec.time,'monotonic',lambda:stamp+122)
    refresh();round_message(messages,3)
    assert thoughts(project(messages,'openai_chat',record=False).messages)==[
        'ROUND1_SENTINEL','ROUND2_SENTINEL','ROUND3_SENTINEL']


def test_failed_metadata_cannot_grant_history_to_new_identity(monkeypatch):
    refresh();stamp=next(iter(ec._schema_cache.values()))['at']
    monkeypatch.setattr(ec.time,'monotonic',lambda:stamp+61)
    def unavailable(_):raise OSError('metadata unavailable')
    rows=[{'endpoint_id':'managed-test','base_url':BASE,'models':[MODEL,'another-model']}]
    ec.refresh_json_schema(rows,fetch_openapi=unavailable)
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']
    # Even the same model/base under a new managed identity needs verification.
    refresh()
    rows=[{'endpoint_id':'replacement','base_url':BASE,'models':[MODEL]}]
    ec.refresh_json_schema(rows,fetch_openapi=unavailable)
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']


def test_valid_schema_withdrawal_retires_history_proof(monkeypatch):
    refresh();stamp=next(iter(ec._schema_cache.values()))['at']
    monkeypatch.setattr(ec.time,'monotonic',lambda:stamp+61)
    doc=document()
    doc['components']['schemas']['ChatCompletionRequest']['properties'].pop('thinking_token_budget')
    refresh(doc=doc)
    assert not ec.interleaved_history_capability(BASE,MODEL)['interleaved_history']

def test_context_estimate_counts_retained_fields_for_each_candidate():
    from src.model_context import estimate_tokens
    refresh();messages=[{'role':'user','content':'current user'}]
    round_message(messages,1);round_message(messages,2)
    for item in messages:
        if item.get('reasoning_content'):item['reasoning_content']='r'*10000
    same=_history_for_candidate(messages,BASE,MODEL)
    other=_history_for_candidate(messages,'http://127.0.0.1:39160/v1',MODEL)
    assert estimate_tokens(same)-estimate_tokens(other)==6000
    assert estimate_tokens(same)==estimate_tokens(project(same,'openai_chat',record=False).messages)

@pytest.mark.parametrize('mode',['sync','async','stream'])
@pytest.mark.parametrize('clear',[None,False])
def test_real_wire_keeps_rounds_and_explicit_clear(monkeypatch,mode,clear):
    from tests.test_sparks_sampling import private_model
    from src import llm_core as core,model_backend
    with private_model() as (url,requests):
        refresh(base=url);ec._cache[url[:-3]]=(time.monotonic(),True)
        monkeypatch.setattr(core,'is_sparks_url',lambda base:base==url)
        monkeypatch.setattr(core,'_model_load_defaults',lambda *a:{})
        monkeypatch.setattr(core,'_ollama_model_caps',lambda *a:None)
        monkeypatch.setattr(core,'_is_host_dead',lambda *a:False)
        monkeypatch.setattr(core,'note_model_activity',lambda *a:None)
        monkeypatch.setattr(core,'_schedule_heal_on_garbage',lambda *a:None)
        monkeypatch.setattr(model_backend,'serving_backend',lambda *a,**k:{'backend':'unknown'})
        if clear is False:
            # Existing native payload choice, injected at the adapter boundary;
            # this does not claim a new public gen_overrides/UI option.
            apply=core._apply_verified_engine_controls
            def configured(payload,*a,**kw):
                payload.setdefault('chat_template_kwargs',{})['clear_thinking']=False
                return apply(payload,*a,**kw)
            monkeypatch.setattr(core,'_apply_verified_engine_controls',configured)
        @asynccontextmanager
        async def slot(*a):yield
        monkeypatch.setattr(core,'_local_model_slot',slot)
        core._response_cache.clear()
        messages=[{'role':'user','content':'previous user'},
                  {'role':'assistant','content':'previous answer','reasoning_content':'OLD_SENTINEL'},
                  {'role':'user','content':'current user'}]
        round_message(messages,1,base=url);round_message(messages,2,base=url)
        before=deepcopy(messages)
        outgoing=_history_for_candidate(messages,url,MODEL)
        if mode=='sync':assert core.llm_call(url,MODEL,outgoing,timeout=5,max_tokens=256)=='READY'
        elif mode=='async':assert asyncio.run(core.llm_call_async(url,MODEL,outgoing,timeout=5,max_tokens=256,max_retries=0,
                    gen_overrides={'think':True,'reasoning_effort':'high','reasoning_budget':128}))=='READY'
        else:
            async def run():return [x async for x in core.stream_llm(url,MODEL,outgoing,timeout=5,max_tokens=256,
                      gen_overrides={'think':True,'reasoning_effort':'high','reasoning_budget':128})]
            assert any('READY' in x for x in asyncio.run(run()))
        body=next(r['body']for r in requests if r['path'].endswith('/chat/completions'))
        assert thoughts(body['messages'])==['OLD_SENTINEL','ROUND1_SENTINEL','ROUND2_SENTINEL']
        assert all('_reasoning_origin' not in item for item in body['messages'])
        assert body['chat_template_kwargs']['clear_thinking'] is (True if clear is None else False)
        assert messages==before
        if path:=os.environ.get('HISTORY859_WIRE_RECEIPT'):
            with Path(path).open('a',encoding='utf-8')as f:f.write(json.dumps({'mode':mode,'clear':clear,'body':body})+'\n')

@contextmanager
def interleaved_backend(*,fail_final=False,final_only=False):
    """Native OpenAI TCP fixture; tool progression comes from actual history."""
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def send(self,value,status=200,kind='application/json'):
            raw=value if isinstance(value,bytes) else json.dumps(value).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            self.send(document() if self.path=='/openapi.json' else {'object':'list','data':[{'id':MODEL,'context_length':32768}]})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if not self.path.endswith('/chat/completions'):
                self.send({'tokens':[1]*10});return
            requests.append(body)
            n=sum(1 for x in body['messages'] if x.get('role')=='tool')
            if fail_final and n>=2:self.send({'error':{'message':'Private fixture rejects final request'}},400);return
            if not body.get('stream'):
                self.send({'model':MODEL,'choices':[{'message':{'content':'READY'},'finish_reason':'stop'}]});return
            if n<2 and not final_only:
                call={'index':0,'id':f'c{n+1}','type':'function','function':{'name':'read_file','arguments':json.dumps({'path':f'file{n+1}.txt'})}}
                deltas=[{'reasoning_content':f'ROUND{n+1}_SENTINEL'},{'tool_calls':[call]}];finish='tool_calls'
            else:deltas=[{'content':'READY'}];finish='stop'
            rows=[{'model':MODEL,'choices':[{'delta':v,'finish_reason':None}]}for v in deltas]
            rows.extend([{'model':MODEL,'choices':[{'delta':{},'finish_reason':finish}]},{'choices':[],'usage':{'prompt_tokens':50,'completion_tokens':10}}])
            raw=(''.join('data: '+json.dumps(row)+'\n\n'for row in rows)+'data: [DONE]\n\n').encode()
            self.send(raw,kind='text/event-stream')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',requests
    finally:server.shutdown();server.server_close();thread.join(2)

@pytest.mark.parametrize('fallback',[False,True])
def test_actual_agent_multiround_and_route_fallback_tcp(monkeypatch,tmp_path,fallback):
    import src.agent_loop as loop
    from src import llm_core as core
    from tests.test_agent_harness_loop import _patch_common
    _patch_common(monkeypatch)
    async def provider(*a,**kw):
        async for chunk in core.stream_llm_with_fallback(*a,**kw,max_retries=0):yield chunk
    monkeypatch.setattr(loop,'stream_llm_with_fallback',provider)
    with interleaved_backend(fail_final=fallback)as(primary,first),interleaved_backend(final_only=True)as(backup,second):
        refresh(base=primary);ec._cache[primary[:-3]]=(time.monotonic(),True)
        monkeypatch.setattr(core,'is_sparks_url',lambda url:url==primary)
        async def run():
            return [chunk async for chunk in loop.stream_agent_loop(primary,MODEL,
                [{'role':'user','content':'Read two files using read_file, then answer READY.'}],
                max_rounds=3,context_length=32768,relevant_tools={'read_file'},workspace=str(tmp_path),
                fallbacks=[(backup,'fallback-model',None)] if fallback else None,fallback_statuses=[400],
                harness_options={'checkpoints':False,'run_tests':False,'repo_map':False})]
        events=''.join(asyncio.run(run()))
        assert len(first)==3,events
        assert thoughts(first[-1]['messages'])[-2:]==['ROUND1_SENTINEL','ROUND2_SENTINEL']
        assert 'READY' in events
        if fallback:
            assert len(second)==1
            assert all(not m.get('reasoning_content') for m in second[0]['messages'])
        else:assert not second
        if path:=os.environ.get('HISTORY859_WIRE_RECEIPT'):
            with Path(path).open('a',encoding='utf-8')as f:f.write(json.dumps({'mode':'agent','fallback':fallback,'bodies':first,'fallback_bodies':second})+'\n')
