from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import instruction_portability_routes as routes
from services.memory.skills import SkillsManager


def fixture(tmp_path, monkeypatch, owner=None):
    manager = SkillsManager(str(tmp_path / 'data'))
    manager.add_skill(name='native-fixture', description='Use for native portability fixtures.',
                      procedure=['Compare the actual output.'], verification=['Expected text exists.'], owner=owner)
    monkeypatch.setattr(routes.agent_defs, 'load_all', lambda workspace: SimpleNamespace(agents=[], errors=[]))
    app = FastAPI()
    if owner:
        @app.middleware('http')
        async def own(request, call_next):
            if not hasattr(request.state, 'current_user'):
                request.state.current_user = owner
            return await call_next(request)
    app.include_router(routes.setup_instruction_portability_routes(manager))
    return TestClient(app), manager


def test_native_canonical_export_routes_preserve_source_and_conflicts(tmp_path, monkeypatch):
    client, manager = fixture(tmp_path, monkeypatch)
    original = manager.load()[0]
    source = Path(original['path']).read_bytes()
    workspace = tmp_path / 'project'; (workspace / '.faustus' / 'rules').mkdir(parents=True)
    (workspace / '.faustus' / 'rules' / 'own.md').write_text('---\npaths: ["src/**"]\n---\nPreserve existing behavior.\n', encoding='utf8')
    catalog = client.get('/api/instruction-portability/catalog', params={'workspace': str(workspace)}).json()
    assert catalog['skills'][0]['id'] == 'native-fixture' and len(catalog['rules']) == 1
    body = {'target':'codex','workspace':str(workspace),'destination':str(tmp_path/'export'),'agents':[]}
    preview = client.post('/api/instruction-portability/preview', json=body)
    assert preview.status_code == 200 and len(preview.json()['files']) >= 2
    written = client.post('/api/instruction-portability/export',json=body).json()
    assert written['created'] == len(preview.json()['files'])
    target = tmp_path/'export'/preview.json()['files'][0]['path']; target.write_text('Preserve destination edit',encoding='utf8')
    repeated = client.post('/api/instruction-portability/export',json=body).json()
    assert len(repeated['conflicts']) == 1 and target.read_text() == 'Preserve destination edit'
    assert Path(original['path']).read_bytes() == source


def test_native_hooks_propose_save_load_evaluate_advisory(tmp_path, monkeypatch):
    client, _ = fixture(tmp_path, monkeypatch)
    proposed = client.post('/api/instruction-portability/hooks/propose',json={'messages':[{'role':'user','content':'No uses `git add -A`; conserva los cambios ajenos.'}]}).json()
    assert len(proposed['proposals']) == 1 and not (tmp_path/'.faustus').exists()
    rule = proposed['proposals'][0]
    saved = client.post('/api/instruction-portability/hooks/save',json={'workspace':str(tmp_path),'rule':rule})
    assert saved.status_code == 200 and saved.json()['status'] == 'saved'
    loaded = client.get('/api/instruction-portability/hooks',params={'workspace':str(tmp_path)}).json()
    assert loaded['rules'][0]['conditions'][0]['pattern'] == 'git add -A'
    evaluated = client.post('/api/instruction-portability/hooks/evaluate',json={'workspace':str(tmp_path),'event':'bash','context':{'command':'git add -A'}}).json()
    assert len(evaluated['matched']) == 1 and evaluated['advisory'] and evaluated['blocked'] is False
    assert client.post('/api/instruction-portability/hooks/evaluate',json={'workspace':str(tmp_path),'event':'bash','context':{'command':'git status'}}).json()['matched'] == []


def test_empty_selection_and_owner_filter_match_canonical_store(tmp_path, monkeypatch):
    client, manager = fixture(tmp_path,monkeypatch,owner='owner-fixture')
    manager.add_skill(name='other-owned',description='Other user skill',procedure=['Keep private'],owner='other-fixture')
    catalog=client.get('/api/instruction-portability/catalog').json()
    assert [row['id'] for row in catalog['skills']] == ['native-fixture']
    preview=client.post('/api/instruction-portability/preview',json={'target':'cursor','skills':[],'rules':[],'agents':[]}).json()
    assert preview['files'] == []


def test_bearer_token_exports_its_real_owners_skills(tmp_path,monkeypatch):
    client, manager=fixture(tmp_path,monkeypatch,owner='owner-fixture')
    @client.app.middleware('http')
    async def token_identity(request,call_next):
        request.state.current_user='api'
        request.state.api_token=True
        request.state.api_token_owner='owner-fixture'
        return await call_next(request)
    assert client.get('/api/instruction-portability/catalog').json()['skills'][0]['id']=='native-fixture'


def test_selected_project_agent_text_exports_without_changing_runtime_trust(tmp_path,monkeypatch):
    client, _=fixture(tmp_path,monkeypatch)
    folder=tmp_path/'project'/'.faustus'/'agents';folder.mkdir(parents=True)
    (folder/'parent.md').write_text('---\nname: Parent\ndescription: Parent fixture\nmode: worker\n---\nPreserve the parent procedure.\n',encoding='utf8')
    (folder/'child.md').write_text('---\nname: Child\ndescription: Child fixture\nextends: parent\n---\nPreserve the child procedure.\n',encoding='utf8')
    before=list((tmp_path/'data').rglob('*'))
    response=client.post('/api/instruction-portability/preview',json={'target':'claude','workspace':str(folder.parent.parent),'skills':[],'rules':[],'agents':['child']})
    assert response.status_code==200
    exported=response.json()['files'][0]['content']
    assert 'Preserve the parent procedure.' in exported and 'Preserve the child procedure.' in exported
    assert list((tmp_path/'data').rglob('*'))==before


def test_mcp_standard_url_and_token_file_aliases_use_existing_credentials(tmp_path,monkeypatch):
    import asyncio
    import httpx
    import json
    from mcp_servers.instruction_portability import server
    token=tmp_path/'owned-token.txt';token.write_text('owned-fixture-token\n',encoding='utf8')
    for key in ('FAUSTUS_INSTRUCTIONS_API_URL','FAUSTUS_API_URL','FAUSTUS_API_TOKEN','FAUSTUS_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('FAUSTUS_URL','http://owned-faustus.invalid')
    monkeypatch.setenv('FAUSTUS_TOKEN_FILE',str(token))
    def respond(request):
        assert str(request.url)=='http://owned-faustus.invalid/api/instruction-portability/catalog'
        assert request.headers['Authorization']=='Bearer owned-fixture-token'
        return httpx.Response(200,json={'skills':[],'rules':[],'agents':[]})
    original=httpx.AsyncClient
    monkeypatch.setattr(server.httpx,'AsyncClient',lambda:original(transport=httpx.MockTransport(respond)))
    result=asyncio.run(server.call_tool('instruction_catalog',{}))
    assert json.loads(result[0].text)['skills']==[]


def test_actual_builtin_startup_schedules_the_portability_server(monkeypatch):
    import asyncio
    from src import builtin_mcp
    calls, pending=[],[]
    class Manager:
        async def connect_server(self,**kwargs):
            calls.append(kwargs)
            return True
    def schedule(coroutine):
        if coroutine.cr_code.co_name=='_start_npx_servers':
            coroutine.close()
        else:
            pending.append(coroutine)
    monkeypatch.setattr(builtin_mcp,'MCP_DISABLED',False)
    monkeypatch.setattr(builtin_mcp,'get_app_root',lambda:str(Path(__file__).resolve().parents[1]))
    monkeypatch.setattr(builtin_mcp,'_spawn_bg',schedule)
    monkeypatch.setattr(builtin_mcp,'_find_npx',lambda:'owned-unused-npx')
    async def run():
        await builtin_mcp.register_builtin_servers(Manager())
        await asyncio.gather(*pending)
    asyncio.run(run())
    own=next(call for call in calls if call['server_id']=='instruction_portability')
    assert own['transport']=='stdio'
    assert Path(own['args'][0]).is_file() and own['args'][0].replace('\\','/').endswith('/mcp_servers/instruction_portability/server.py')
    assert 'instruction_portability' not in builtin_mcp.NATIVE_TWIN_SERVERS
