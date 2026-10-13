import asyncio
from types import SimpleNamespace
from src.coordination.bindings import Binding, ProjectTemplate
from src.coordination.client import Client
from routes.coordination_routes import SaveBinding


def test_existing_bindings_default_to_ecosystem_and_project_space_is_inherited():
    base = dict(id='template',owner='',project_id='native-project',connector_id='hub',actor='faustus-child',operations=('board',))
    assert ProjectTemplate(**base).for_session('old').space_id == 'ecosystem'
    template = ProjectTemplate(**base,space_id='project-a')
    assert template.for_session('new').space_id == 'project-a'
    assert SaveBinding(connector_id='hub',space_id='project-a').space_id == 'project-a'
    assert SaveBinding(connector_id='hub').space_id == 'ecosystem'


def test_native_client_uses_binding_space_even_when_mcp_default_differs():
    binding = Binding(id='binding',owner='',project_id='native-project',session_id='child',
        connector_id='hub',actor='faustus-child',operations=('board',),space_id='project-a')
    calls = []
    class Manager:
        def get_all_tools(self):return [{'qualified_name':'mcp__hub__hub_agora_board'}]
        async def call_tool(self,name,args):
            calls.append(args)
            return {'structured_content':{'ok':True,'space_id':args.get('space_id','ecosystem')}}
    client = Client(SimpleNamespace(live=lambda binding:None),binding,Manager(),revalidate=lambda binding:None)
    result = asyncio.run(client.remote('board',{'agent':'faustus-child'}))
    assert result['space_id'] == 'project-a'
    assert calls == [{'agent':'faustus-child','space_id':'project-a'}]
