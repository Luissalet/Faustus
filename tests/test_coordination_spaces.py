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


def test_switching_space_restores_separate_cursor_details_and_task_observations(tmp_path):
    from dataclasses import replace
    import pytest
    from src.coordination.bindings import CoordinationError
    from src.coordination.store import Store
    store = Store(tmp_path)
    a = store.save(Binding(id='same-binding',owner='',project_id='project',session_id='session',
        connector_id='hub',actor='faustus-private',operations=('poll','task'),space_id='project-a'))
    store.receive(a,expected_cursor=0,next_cursor=100,
        messages=[{'id':100,'thread_id':32,'body':'Only A','author':'tester'}])
    receipt = store.observe_task(a,{'id':7,'thread_id':32,'title':'Only A task'})
    store.confirm_task_delivery(a,receipt)
    b = store.save(replace(a,space_id='project-b'),expected_revision=a.revision)
    assert store.cursor(b) == 0 and not store.pending(b)
    with pytest.raises(CoordinationError):store.observed_task(b,7)
    with pytest.raises(CoordinationError):store.confirm_task_delivery(b,receipt)
    store.receive(b,expected_cursor=0,next_cursor=3,
        messages=[{'id':3,'thread_id':32,'body':'Only B','author':'tester'}])
    back = store.save(replace(b,space_id='project-a'),expected_revision=b.revision)
    assert store.cursor(back) == 100
    assert store.pending(back)[0]['body_excerpt'] == 'Only A'
    assert store.observed_task(back,7)['title'] == 'Only A task'
