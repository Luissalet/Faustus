"""Inert HTTP contract checks; no Main, product DB, provider, model or Hub."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from routes import global_model_routes as route


def endpoint(**changes):
    values = dict(id='selected-id', connection_revision='connection-r1', base_url='https://provider.invalid/v1',
                  api_key='synthetic-provider-secret', provider_auth_id=None, endpoint_kind='openai',
                  owner='alice', is_enabled=True, hidden_models='[]')
    return SimpleNamespace(**(values | changes))


class GlobalModelRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.row = endpoint()
        self.requests = []
        self.mode = 'cookie'
        self.scopes = ['chat']
        self.owner = 'alice'
        self.peer = lambda request: httpx.Response(200, content=b'{"native":"unchanged"}',
             headers={'content-type':'application/json', 'set-cookie':'must-not-relay=1'})
        self.patches = [patch.object(route, '_auth_disabled', return_value=False),
                        patch.object(route, '_vision', return_value=True),
                        patch.object(route, '_read_target', side_effect=lambda *args:(self.row,'selected-model')),
                        patch.object(route, '_runtime', side_effect=lambda *args:('https://provider.invalid/v1/chat/completions',
                              {'Authorization':'Bearer synthetic-provider-secret'})),
                        patch.object(route, '_client', side_effect=self.provider),
                        patch('socket.socket.connect', side_effect=AssertionError('No real network in these tests'))]
        for item in self.patches:
            item.start()
        self.app = FastAPI()
        @self.app.middleware('http')
        async def identity(request, call_next):
            request.state.current_user = 'alice'
            if self.mode == 'token':
                request.state.api_token = True
                request.state.api_token_owner = self.owner
                request.state.api_token_scopes = self.scopes
            return await call_next(request)
        self.app.include_router(route.setup_global_model_routes(), prefix='/api')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app),base_url='http://review.invalid')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    def provider(self):
        async def handle(request):
            self.requests.append(request)
            return self.peer(request)
        return httpx.AsyncClient(transport=httpx.MockTransport(handle),trust_env=False,follow_redirects=False)

    def body(self, **changes):
        return dict(expected_endpoint_id='selected-id',expected_model='selected-model',
                    expected_connection_revision='connection-r1',model='selected-model',
                    messages=[{'role':'user','content':'fixture'}]) | changes

    async def post(self, **changes):
        return await self.client.post('/api/global-model/v1/chat/completions',json=self.body(**changes))

    async def test_descriptor_never_resolves_or_exposes_credentials(self):
        response = await self.client.get('/api/global-model')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'endpoint_id':'selected-id','model':'selected-model',
                         'connection_revision':'connection-r1','chat_path':'/api/global-model/v1/chat/completions','protocol':'openai','vision':True})
        route._runtime.assert_not_called()
        self.assertNotIn('synthetic-provider-secret',response.text)

    async def test_tokens_need_chat_scope_and_real_owner_even_without_middleware_matrix(self):
        self.mode = 'token'
        self.scopes = []
        self.assertEqual((await self.post()).status_code,403)
        self.scopes = ['chat']
        self.owner = None
        self.assertEqual((await self.client.get('/api/global-model')).status_code,403)
        self.owner = 'alice'
        self.assertEqual((await self.post()).status_code,200)

    async def test_pin_mismatch_does_not_send_or_resolve_provider_credentials(self):
        for field in ('expected_endpoint_id','expected_model','expected_connection_revision','model'):
            with self.subTest(field=field):
                response = await self.post(**{field:'different'})
                self.assertEqual(response.status_code,409)
        self.assertEqual(self.requests,[])
        route._runtime.assert_not_called()

    async def test_connection_changed_during_credential_resolution_never_sends(self):
        route._read_target.side_effect = [(self.row,'selected-model'),(endpoint(connection_revision='connection-r2'),'selected-model')]
        self.assertEqual((await self.post()).status_code,409)
        self.assertFalse(self.requests)

    async def test_global_pick_changed_during_credential_resolution_never_sends(self):
        route._read_target.side_effect = [(self.row,'selected-model'),(self.row,'new-model')]
        self.assertEqual((await self.post()).status_code,409)
        self.assertFalse(self.requests)

    async def test_multimodal_schema_payload_and_only_provider_auth_are_preserved(self):
        messages=[{'role':'user','content':[{'type':'text','text':'Imagen ñ'},
                  {'type':'image_url','image_url':{'url':'data:image/png;base64,cGl4ZWxz'}}]}]
        schema={'type':'json_schema','json_schema':{'name':'native','schema':{'type':'object','additionalProperties':False}}}
        body=self.body(messages=messages,response_format=schema,stream=False,temperature=0,chat_template_kwargs={'enable_thinking':False})
        response=await self.client.post('/api/global-model/v1/chat/completions',json=body,headers={'Authorization':'Bearer caller-secret','X-Unsafe':'do-not-forward'})
        self.assertEqual(response.content,b'{"native":"unchanged"}')
        self.assertNotIn('set-cookie',response.headers)
        self.assertEqual(len(self.requests),1)
        forwarded=json.loads(self.requests[0].content)
        self.assertEqual(forwarded,{key:value for key,value in body.items() if key not in route._EXPECTED})
        self.assertEqual(self.requests[0].headers['authorization'],'Bearer synthetic-provider-secret')
        self.assertNotIn('x-unsafe',self.requests[0].headers)

    async def test_provider_error_status_body_retry_after_and_schema_are_not_retried(self):
        self.peer=lambda request:httpx.Response(422,content=b'{"error":"schema unsupported"}',
                      headers={'content-type':'application/json','retry-after':'8'})
        response=await self.post(response_format={'type':'json_schema','json_schema':{'schema':{}}})
        self.assertEqual(response.status_code,422)
        self.assertEqual(response.content,b'{"error":"schema unsupported"}')
        self.assertEqual(response.headers['retry-after'],'8')
        self.assertEqual(len(self.requests),1)
        self.assertIn('response_format',json.loads(self.requests[0].content))

    async def test_redirect_is_returned_without_following_or_leaking_location(self):
        self.peer=lambda request:httpx.Response(307,headers={'location':'https://other.invalid/stolen'})
        response=await self.post()
        self.assertEqual(response.status_code,307)
        self.assertNotIn('location',response.headers)
        self.assertEqual(len(self.requests),1)

    async def test_arbitrary_destination_and_invalid_shapes_do_not_dispatch(self):
        for changes in ({'url':'http://arbitrary.invalid'}, {'messages':[]}, {'stream':'true'},
                        {'expected_connection_revision':None}):
            self.assertEqual((await self.post(**changes)).status_code,422)
        self.assertFalse(self.requests)

    async def test_transport_exception_does_not_expose_url_or_credentials(self):
        def fail(request):
            raise httpx.ConnectError('https://secret.invalid synthetic-provider-secret',request=request)
        self.peer=fail
        response=await self.post()
        self.assertEqual(response.status_code,502)
        self.assertNotIn('synthetic-provider-secret',response.text)
        self.assertNotIn('secret.invalid',response.text)

    async def test_sse_bytes_and_native_finish_are_not_rewritten(self):
        raw=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.peer=lambda request:httpx.Response(200,content=raw,headers={'content-type':'text/event-stream'})
        response=await self.post(stream=True)
        self.assertEqual(response.content,raw)
        self.assertEqual(response.headers['content-type'],'text/event-stream')

    async def test_downstream_cancellation_closes_retained_upstream_before_returning(self):
        closed=[]
        class Held(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'first chunk'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.append(True)
        self.peer=lambda request:httpx.Response(200,stream=Held(),headers={'content-type':'text/event-stream'})
        body=json.dumps(self.body(stream=True)).encode()
        sent=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':body,'more_body':False}
            await asyncio.Event().wait()
        async def send(message):
            if message['type']=='http.response.body' and message.get('body'):
                raise asyncio.CancelledError
        # Direct router endpoint avoids a TestClient that buffers the whole body.
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','method':'POST',
               'scheme':'http','path':'/api/global-model/v1/chat/completions','raw_path':b'/api/global-model/v1/chat/completions',
               'query_string':b'','headers':[(b'content-type',b'application/json')],
               'client':('127.0.0.1',1),'server':('review.invalid',80),'state':{'current_user':'alice'}}
        plain=FastAPI()
        plain.include_router(route.setup_global_model_routes(),prefix='/api')
        with self.assertRaises(asyncio.CancelledError):
            await plain(scope,receive,send)
        self.assertEqual(closed,[True])

    async def test_disconnect_before_provider_headers_cancels_the_request_and_closes_client(self):
        entered=asyncio.Event()
        cancelled=[]
        clients_closed=[]
        async def peer(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        class Client(httpx.AsyncClient):
            async def aclose(self):
                await super().aclose()
                clients_closed.append(True)
        route._client.side_effect=lambda:Client(transport=httpx.MockTransport(peer))
        sent=False
        body=json.dumps(self.body()).encode()
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':body,'more_body':False}
            await entered.wait()
            return {'type':'http.disconnect'}
        replies=[]
        async def send(message):replies.append(message)
        app=FastAPI()
        app.include_router(route.setup_global_model_routes(),prefix='/api')
        await app({'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
             'method':'POST','scheme':'http','path':'/api/global-model/v1/chat/completions',
             'query_string':b'','headers':[(b'content-type',b'application/json')],
             'client':('127.0.0.1',1),'server':('review.invalid',80),'state':{'current_user':'alice'}},receive,send)
        self.assertEqual(cancelled,[True])
        self.assertEqual(clients_closed,[True])
        self.assertEqual(replies[0]['status'],499)


class SelectionTests(unittest.TestCase):
    def read(self, row, *, owner='alice', admin=False, settings=None):
        db=SimpleNamespace(query=lambda model:SimpleNamespace(filter=lambda expression:SimpleNamespace(first=lambda:row)))
        class Session:
            def __enter__(self):return db
            def __exit__(self,*args):pass
        database=ModuleType('core.database')
        database.ModelEndpoint=SimpleNamespace(id='id')
        database.SessionLocal=Session
        config=ModuleType('src.settings')
        config.load_settings=lambda:settings if settings is not None else {'default_endpoint_id':'selected-id','default_model':'selected-model'}
        with patch.dict(sys.modules,{'core.database':database,'src.settings':config}):
            return route._read_target(owner,admin)

    def test_visibility_disabled_hidden_and_missing_selection_never_substitute(self):
        for row,settings,status in [(endpoint(owner='bob'),None,404),(endpoint(is_enabled=False),None,503),
                                   (endpoint(hidden_models='["selected-model"]'),None,503),(None,None,404),
                                   (endpoint(),{},503)]:
            with self.subTest(status=status):
                with self.assertRaises(HTTPException) as raised:self.read(row,settings=settings)
                self.assertEqual(raised.exception.status_code,status)
        row,model=self.read(endpoint(owner=None))
        self.assertEqual((row.id,model),('selected-id','selected-model'))

    def test_authz_matrix_only_admits_exact_routes_with_chat_scope(self):
        spec=importlib.util.spec_from_file_location('isolated_authz938',Path(__file__).resolve().parents[1]/'core/authz.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'isolated_authz938':module}):
            spec.loader.exec_module(module)
        api_rule_for=module.api_rule_for
        for method,path in [('GET','/api/global-model'),('POST','/api/global-model/v1/chat/completions')]:
            rule=api_rule_for(method,path)
            self.assertEqual(rule.scopes,('chat',))
        self.assertIsNone(api_rule_for('POST','/api/global-model/admin'))

    def test_runtime_uses_server_credential_resolution_and_preserves_conflicts(self):
        resolver=ModuleType('src.endpoint_resolver')
        class Changed(RuntimeError):pass
        resolver.EndpointConfigurationChanged=Changed
        calls=[]
        resolver.resolve_endpoint_runtime=lambda row,owner:(calls.append((row.id,owner)) or ('https://provider.invalid/v1','resolved-secret'))
        resolver.build_chat_url=lambda base:base+'/chat/completions'
        resolver.build_headers=lambda key,base:{'Authorization':'Bearer '+key}
        with patch.dict(sys.modules,{'src.endpoint_resolver':resolver}):
            url,headers=route._runtime(endpoint(),'alice')
            self.assertEqual(calls,[('selected-id','alice')])
            self.assertEqual(headers,{'Authorization':'Bearer resolved-secret'})
            self.assertEqual(url,'https://provider.invalid/v1/chat/completions')
            def changed(*args,**kwargs):raise Changed('private secret must not escape')
            resolver.resolve_endpoint_runtime=changed
            with self.assertRaises(HTTPException) as raised:route._runtime(endpoint(),'alice')
            self.assertEqual(raised.exception.status_code,409)
            self.assertNotIn('private secret',str(raised.exception.detail))


if __name__ == '__main__':
    unittest.main()
