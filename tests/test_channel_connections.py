"""Native provider contracts, opaque IDs, cursor retention and effect results."""
import json
from pathlib import Path
import pytest

from src import channel_connections as channels, integrations


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "STORE", tmp_path / "channels.json")
    monkeypatch.setattr(integrations, "get_integration", lambda identifier: {"id": identifier, "api_key": "private-fixture"})
    return {provider: channels.save_connection(provider, provider) for provider in channels.PROVIDERS}


def test_profiles_reference_existing_credential_without_copying_it(profiles):
    rows = channels.list_connections()
    assert len(rows) == 3
    assert all(row["credential_registered"] for row in rows)
    assert "private-fixture" not in channels.STORE.read_text()
    original = rows[0]
    edited = channels.save_connection(original["provider"], "replacement", "Changed", original["id"])
    assert edited["id"] == original["id"]
    assert len(channels.list_connections()) == 3


def test_corrupt_store_is_not_overwritten(tmp_path, monkeypatch):
    store = tmp_path / 'broken.json';store.write_text('{broken',encoding='utf-8')
    monkeypatch.setattr(channels,'STORE',store)
    monkeypatch.setattr(integrations,'get_integration',lambda identifier:{'id':identifier})
    with pytest.raises(ValueError):channels.save_connection('slack','fixture')
    assert store.read_text()=='{broken'


@pytest.mark.parametrize('provider', ['slack','discord','matrix'])
async def test_message_history_keeps_text_ids_attachments_and_cursors(profiles,monkeypatch,provider):
    text='á'*16000
    bodies={
        'slack':{'ok':True,'messages':[{'ts':'001.000001','text':text,'user':'u','files':[{'id':'f'}]}], 'response_metadata':{'next_cursor':'opaque='}},
        'discord':[{'id':'00123','content':text,'author':{'id':'u'},'attachments':[{'id':'f'}]}],
        'matrix':{'chunk':[{'event_id':'$00123','sender':'@u:fixture','content':{'body':text,'url':'mxc://fixture/f'}}],'end':'opaque='},
    }
    requests=[]
    async def call(identifier,method,path,**kwargs):
        requests.append((method,path,kwargs));return {'exit_code':0,'data':bodies[provider]}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.history(profiles[provider]['id'],'!room:fixture','cursor=input',limit=1)
    assert result['messages'][0]['text']==text
    assert result['messages'][0]['id'] in ('001.000001','00123','$00123')
    assert result['messages'][0]['attachments']
    assert result['next_cursor']==('00123' if provider=='discord' else 'opaque=')
    assert requests[0][2]['return_data'] is True
    assert result['untrusted_content'] is True


@pytest.mark.parametrize('provider',['slack','discord','matrix'])
async def test_send_and_edit_use_provider_contract_and_return_actual_id(profiles,monkeypatch,provider):
    requests=[]
    response={'slack':{'ok':True,'ts':'001.000002'},'discord':{'id':'00099'},'matrix':{'event_id':'$new'}}[provider]
    async def call(identifier,method,path,**kwargs):
        requests.append((method,path,kwargs));return {'exit_code':0,'data':response}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    first=await channels.write_message(profiles[provider]['id'],'!room:fixture','new')
    second=await channels.write_message(profiles[provider]['id'],'!room:fixture','edited','old','txn=1')
    assert first['action']=='send' and second['action']=='edit'
    assert first['message_id'] in ('001.000002','00099','$new')
    if provider=='slack':assert requests[1][1]=='/api/chat.update' and requests[1][2]['body']['ts']=='old'
    elif provider=='discord':assert requests[1][0]=='PATCH' and requests[1][1].endswith('/messages/old')
    else:
        assert requests[1][0]=='PUT' and requests[1][1].endswith('/txn%3D1')
        assert requests[1][2]['body']['m.relates_to']=={'rel_type':'m.replace','event_id':'old'}


@pytest.mark.parametrize('provider',['slack','discord','matrix'])
async def test_unknown_send_outcome_is_not_replayed(profiles,monkeypatch,provider):
    requests=[]
    async def call(*args,**kwargs):
        requests.append(True);return {'exit_code':1,'outcome_unknown':True,'status':'outcome_unknown','error':'lost reply'}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.write_message(profiles[provider]['id'],'channel','text')
    assert result['outcome_unknown'] is True
    assert len(requests)==1


async def test_slack_service_error_is_not_a_successful_http_200(profiles,monkeypatch):
    async def call(*args,**kwargs):return {'exit_code':0,'data':{'ok':False,'error':'channel_not_found'}}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.list_spaces(profiles['slack']['id'])
    assert result['exit_code']==1 and result['error']=='channel_not_found'


@pytest.mark.parametrize('provider',['slack','discord','matrix'])
async def test_missing_write_receipt_remains_uncertain(profiles,monkeypatch,provider):
    calls=[]
    async def call(*args,**kwargs):
        calls.append(True);return {'exit_code':0,'data':{'ok':True}}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.write_message(profiles[provider]['id'],'channel','text')
    assert result['exit_code']==1 and result['outcome_unknown'] is True
    assert len(calls)==1


@pytest.mark.parametrize('provider',['slack','discord','matrix'])
async def test_reactions_use_native_encoding_and_contract(profiles,monkeypatch,provider):
    requests=[]
    async def call(identifier,method,path,**kwargs):
        requests.append((method,path,kwargs));return {'exit_code':0,'data':{'ok':True,'event_id':'$reaction'}}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.react(profiles[provider]['id'],'!room:fixture','001.0001','👍','txn=1')
    assert result['exit_code']==0
    method,path,kwargs=requests[0]
    if provider=='slack':
        assert kwargs['body']=={'channel':'!room:fixture','timestamp':'001.0001','name':'👍'}
    elif provider=='discord':
        assert method=='PUT' and '/reactions/%F0%9F%91%8D/@me' in path
    else:
        assert path.endswith('/m.reaction/txn%3D1')
        assert kwargs['body']['m.relates_to']=={'rel_type':'m.annotation','event_id':'001.0001','key':'👍'}


async def test_discord_guild_pagination_and_channel_listing(profiles,monkeypatch):
    requests=[]
    async def call(identifier,method,path,**kwargs):
        requests.append((path,kwargs));return {'exit_code':0,'data':[{'id':'0000123','name':'Fixture'}]}
    monkeypatch.setattr(integrations,'execute_api_call',call)
    result=await channels.list_spaces(profiles['discord']['id'],cursor='0000100',limit=1)
    assert result['kind']=='workspaces' and result['next_cursor']=='0000123'
    assert requests[0][1]['params']['after']=='0000100'
    result=await channels.list_spaces(profiles['discord']['id'],parent_id='0000123')
    assert result['kind']=='channels' and result['next_cursor']==''
    assert requests[1][0]=='/api/v10/guilds/0000123/channels'


async def test_native_edit_route_rejects_missing_message_id_without_sending(profiles,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from routes.channel_connection_routes import setup_channel_connection_routes
    from routes import channel_connection_routes
    app=FastAPI();app.include_router(setup_channel_connection_routes())
    monkeypatch.setattr(channel_connection_routes,'require_admin',lambda request:None)
    calls=[]
    async def write(*args,**kwargs):calls.append(True)
    monkeypatch.setattr(channels,'write_message',write)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
        result=await client.post('/api/channel-connections/run',json={'connection_id':profiles['slack']['id'],'operation':'edit','channel_id':'channel','text':'edited'})
    assert result.status_code==400 and calls==[]
