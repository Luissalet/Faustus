import asyncio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.group_orchestrator import GroupCoordinator, GroupStore
from routes.group_routes import setup_group_routes
from tests.test_group_orchestrator import Invoker, Transcript, definition, idle


@pytest.fixture
def group_api(tmp_path,monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED','false')
    inv=Invoker();inv.release.set()
    transcript=Transcript();coordinator=GroupCoordinator(inv,transcript,store=GroupStore(tmp_path/'groups.db'))
    coordinator.adopt(definition(),None)
    app=FastAPI();app.include_router(setup_group_routes(coordinator))
    return app,coordinator,inv

async def test_http_submit_replay_and_second_client_see_same_completed_work(group_api):
    app,c,inv=group_api
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        first=await client.post('/api/groups/parent/messages',json={'message':'work','request_id':'actual-key'})
        repeat=await client.post('/api/groups/parent/messages',json={'message':'work','request_id':'actual-key'})
        assert first.status_code==repeat.status_code==200
        assert first.json()['turn_id']==repeat.json()['turn_id']
    await idle(c)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as observer:
        result=(await observer.get('/api/groups/parent')).json()
        assert not result['busy'] and len(result['steps'])==2
        assert len((await observer.get('/api/groups')).json()['groups'])==1
        assert (await observer.get('/api/groups/not-owned')).status_code==404
        assert (await observer.post('/api/groups/parent/archive')).status_code==200
        assert (await observer.get('/api/groups')).json()['groups']==[]
    assert len(inv.calls)==2

async def test_bad_mode_cannot_overwrite_saved_definition(group_api):
    app,c,_=group_api
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        bad=definition();bad['mode']='unknown'
        response=await client.post('/api/groups/adopt',json=bad)
        assert response.status_code==400
        assert c.store.group('parent',None)['mode']=='round-robin'

async def test_global_default_is_read_without_exposing_credentials(group_api,monkeypatch):
    app,_,_=group_api
    monkeypatch.setattr('services.hoard_copilot.global_model',lambda owner:{'endpoint_url':'http://spark/v1/chat/completions','model':'configured-model','headers':{'Authorization':'private'}})
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        response=await client.get('/api/groups/config')
        assert response.status_code==200
        assert response.json()['defaultRoute']['model']=='configured-model'
        assert 'private' not in response.text and 'Authorization' not in response.text


async def test_cookie_owner_cannot_change_group_owner_with_internal_header(group_api,monkeypatch):
    app,c,_=group_api
    monkeypatch.setenv('AUTH_ENABLED','true')
    c.adopt(definition('alice-parent'),'alice');c.adopt(definition('bob-parent'),'bob')
    @app.middleware('http')
    async def identity(request,call_next):
        request.state.current_user='alice'
        return await call_next(request)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        assert (await client.get('/api/groups/alice-parent')).status_code==200
        response=await client.get('/api/groups/bob-parent',headers={'X-Faustus-Group-Owner':'bob'})
        assert response.status_code==404

async def test_trusted_native_client_can_use_explicit_owner(group_api,monkeypatch):
    from core.middleware import INTERNAL_TOOL_TOKEN
    app,c,_=group_api;monkeypatch.setenv('AUTH_ENABLED','true')
    c.adopt(definition('bob-parent'),'bob')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        response=await client.get('/api/groups/bob-parent',headers={'X-Odysseus-Internal-Token':INTERNAL_TOOL_TOKEN,'X-Faustus-Group-Owner':'bob'})
        assert response.status_code==200
        assert response.json()['group']['parentId']=='bob-parent'


async def test_unavailable_global_route_stays_selected_instead_of_pc_fallback(group_api,monkeypatch):
    app,_,_=group_api
    monkeypatch.setattr('services.hoard_copilot.global_model',lambda owner:{'endpoint_url':'','model':'','headers':{}})
    monkeypatch.setattr('src.settings.get_setting',lambda key,default=None:{'default_endpoint_id':'sparks-configured','default_model':'configured-global'}.get(key,default))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        route=(await client.get('/api/groups/config')).json()['defaultRoute']
        assert route['endpointId']=='sparks-configured' and route['model']=='configured-global'
        assert route['missing'] and route['endpointUrl']==''


async def test_native_send_exposes_and_persists_debate_mode(group_api):
    app,c,_=group_api
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        result=await client.post('/api/groups/parent/messages',json={'message':'Consolidate','request_id':'debate-key','debate':True})
        assert result.status_code==200
        await idle(c)
        snapshot=(await client.get('/api/groups/parent')).json()
        assert snapshot['group']['debate'] is True
        assert snapshot['turns'][0]['debate'] is True
        assert len([t for t in snapshot['turns'] if t['request_id'].startswith('final:')]) == 1
