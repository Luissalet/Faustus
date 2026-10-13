import json
import httpx
from mcp_servers import sparks_server as sparks

async def test_sparks_native_catalog_covers_model_preferences_and_sync():
    tools={t.name:t for t in await sparks.list_tools()}
    assert len(tools)==4
    assert tools['sparks_status'].annotations.readOnlyHint
    assert not tools['sparks_deploy'].annotations.readOnlyHint
    assert tools['sparks_deploy'].inputSchema['properties']['action']['enum']==['start','stop']

async def test_sparks_mcp_preserves_native_unavailable_selection(monkeypatch):
    seen=[];original=httpx.AsyncClient
    def handle(request):
        seen.append(request)
        return httpx.Response(200,json={'action':'none','endpoint_status':'unavailable','default':{'endpoint_id':'sparks-private','model':'fixture-global'}})
    monkeypatch.setattr(sparks.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL','http://127.0.0.1:63683')
    result=await sparks.call_tool('sparks_sync',{})
    data=json.loads(result[0].text)
    assert data['default']['endpoint_id']=='sparks-private' and data['action']=='none'
    assert seen[0].url.path=='/api/sparks/sync' and seen[0].method=='POST'
