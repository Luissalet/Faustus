import json
import pytest
import httpx
from mcp_servers import groups_server as groups

async def test_native_catalog_exposes_tools_modes_queue_and_pending_questions():
    catalog={t.name:t for t in await groups.list_tools()}
    assert len(catalog)==9
    assert catalog['group_update'].inputSchema['properties']['expected_revision']['type']=='integer'
    assert catalog['group_create'].inputSchema['properties']['executionMode']['enum']==['chat','agent']
    assert catalog['group_send'].inputSchema['properties']['request_id']['type']=='string'
    assert catalog['group_resume'].inputSchema['properties']['answer']['type']=='object'
    assert catalog['group_get'].annotations.readOnlyHint
    assert not catalog['group_send'].annotations.readOnlyHint
    for name in ('group_create', 'group_update'):
        participant = catalog[name].inputSchema['properties']['participants']['items']['properties']
        assert 'read_only' in participant['autonomyPreset']['enum']
        assert participant['inputTokenBudget']['maximum'] == 200000

async def test_mcp_request_targets_own_native_api_and_preserves_ids(monkeypatch):
    captured=[]
    def handle(request):
        captured.append(request)
        return httpx.Response(200,json={'turn_id':'actual-turn','fresh':False})
    original=httpx.AsyncClient
    monkeypatch.setattr(groups.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL','http://127.0.0.1:63401')
    monkeypatch.setenv('FAUSTUS_INTERNAL_TOOL_TOKEN','private-test-token')
    result=await groups.call_tool('group_send',{'group_id':'a/b','message':'original','request_id':'stable'})
    assert json.loads(result[0].text)['turn_id']=='actual-turn'
    assert captured[0].url.path=='/api/groups/a/b/messages'
    assert json.loads(captured[0].content)=={'message':'original','request_id':'stable'}
    assert captured[0].headers['X-Odysseus-Internal-Token']=='private-test-token'

async def test_mcp_does_not_hide_failure_as_completion(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(groups.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(404,json={'detail':'Group not found'})),**kw))
    monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL','http://127.0.0.1:63401')
    result=await groups.call_tool('group_get',{'group_id':'missing'})
    assert json.loads(result[0].text)['exit_code']==1
    assert json.loads(result[0].text)['status_code']==404
