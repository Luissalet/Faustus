import json
import httpx
from mcp_servers import council_server as council

async def test_council_catalog_exposes_native_models_tasks_and_commands():
    tools={tool.name:tool for tool in await council.list_tools()}
    assert len(tools)==12
    assert tools['council_options'].annotations.readOnlyHint
    assert tools['council_send'].inputSchema['properties']['idempotency_key']['type']=='string'
    assert tools['council_update'].inputSchema['required']==['session_id','changes','expected_revision']

async def test_council_transport_uses_native_patch_and_command_shapes(monkeypatch):
    original=httpx.AsyncClient;seen=[]
    def handle(request):
        seen.append(request);return httpx.Response(200,json={'ok':True})
    monkeypatch.setattr(council.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL','http://127.0.0.1:63686')
    await council.call_tool('council_update',{'session_id':'room','changes':{'budgets':{'max_rounds':0}},'expected_revision':3})
    assert json.loads(seen[-1].content)=={'patch':{'budgets':{'max_rounds':0}},'expected_revision':3}
    await council.call_tool('council_command',{'session_id':'room','command':'cancel_turn','arguments':{'turn_id':'turn'}})
    assert json.loads(seen[-1].content)=={'command':'cancel_turn','turn_id':'turn'}

async def test_native_domain_refusal_is_not_reported_as_success(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(council.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'ok':False,'error':{'code':'revision_conflict'}})),**kw))
    monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL','http://127.0.0.1:63686')
    data=json.loads((await council.call_tool('council_command',{'session_id':'room','command':'pause'}))[0].text)
    assert data['exit_code']==1 and data['error']['error']['code']=='revision_conflict'
