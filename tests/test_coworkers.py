import pytest
from src.coworkers import Store, Coworker


def test_private_responsibilities_and_revision_conflicts(tmp_path):
    store=Store(tmp_path)
    first=store.save('alice',Coworker(name='Designer',responsibility='Review design'))
    with pytest.raises(LookupError):
        store.get('bob',first['id'])
    updated=store.save('alice',Coworker(name='Designer',responsibility='Make motion'),first['id'],1)
    assert updated['revision']==2
    with pytest.raises(ValueError):
        store.save('alice',Coworker(name='Designer',responsibility='stale'),first['id'],1)
    assert store.list('bob')==[]


def test_same_request_recovers_receipt_without_resubmitting(tmp_path):
    store=Store(tmp_path)
    row=store.save('a',Coworker(name='Worker',responsibility='Build'))
    assert store.begin('a',row['id'],'request','session','mission') is None
    with pytest.raises(ValueError,match='running mission'):
        store.begin('a',row['id'],'other','session','mission')
    store.finish('a','request',{'output':'Evidence','exit_code':0})
    assert Store(tmp_path).begin('a',row['id'],'request','session','mission')['receipt']['output']=='Evidence'
    with pytest.raises(ValueError,match='another mission'):
        store.begin('a',row['id'],'request','session','different')


def test_paused_coworker_cannot_start(tmp_path):
    store=Store(tmp_path)
    row=store.save(None,Coworker(name='Worker',responsibility='Work',state='paused'))
    with pytest.raises(ValueError,match='Activate'):
        store.begin(None,row['id'],'request','session','mission')

def test_uncertain_work_blocks_repetition_until_owner_inspects_it(tmp_path):
    store=Store(tmp_path)
    row=store.save('alice',Coworker(name='Worker',responsibility='Build'))
    store.begin('alice',row['id'],'one','s','build')
    store.interrupted('alice','one')
    with pytest.raises(ValueError,match='running mission'):
        Store(tmp_path).begin('alice',row['id'],'two','s','build')
    with pytest.raises(LookupError):
        store.resolve('bob',row['id'],'one')
    store.resolve('alice',row['id'],'one')
    assert store.history('alice',row['id'])[0]['state']=='closed'
    assert store.begin('alice',row['id'],'two','s','build') is None

@pytest.mark.asyncio
async def test_mission_reuses_existing_runner_and_identical_security_context(tmp_path, monkeypatch):
    import json
    from src import coworkers
    from src.agent_tools.coworker_tools import CoworkerRunTool
    from src.agent_tools.subagent_tools import DelegateAgentsTool
    monkeypatch.setattr(coworkers,'DATA_DIR',tmp_path)
    row=Store(tmp_path).save('alice',Coworker(name='Worker',responsibility='Verify real results'))
    calls=[]
    async def run(self,content,ctx):
        calls.append((json.loads(content),ctx))
        return {'output':'Evidence from the existing runner','exit_code':0}
    monkeypatch.setattr(DelegateAgentsTool,'execute',run)
    context={'owner':'alice','session_id':'s','permissions':{'tools':['read_file']}}
    args={'id':row['id'],'request_id':'one','mission':'Check the project'}
    await CoworkerRunTool().execute(args,context)
    repeated=await CoworkerRunTool().execute(args,context)
    assert len(calls)==1 and calls[0][1] is context
    assert calls[0][0]['tasks'][0]['agent']=='implementer'
    assert repeated['state']=='finished' and repeated['receipt']['output'].startswith('Evidence')

def test_plain_language_selects_the_real_coworker_tools():
    from src import agent_loop
    intent=agent_loop._classify_agent_request([], 'Crea un compañero para mi taller 3D')
    assert 'coworkers' in intent['domains']
    assert agent_loop._DOMAIN_HOT_TOOLS['coworkers']=={'coworker_list','coworker_save','coworker_run'}


def test_selected_coworkers_have_prompt_rules():
    from src import agent_loop
    selected = {'coworker_list', 'coworker_save', 'coworker_run'}
    rules = agent_loop._domain_rules_for_tools(selected)
    assert len(rules) == 1
    assert 'Coworker rules' in rules[0]
    assert 'request_id' in rules[0]


def test_specialist_templates_cover_the_whole_family():
    from src.coworkers import templates
    rows=templates()
    expected=set('argus atlas babel borges cassandra cicero cookhoard daguerre diskhoard dorian echo funes galton gamerhoard gepetto heron hoardhub homehoard hypatia jobhunter kafka laplace ledger links lumiere mercator midas nightingale people phileas platos prospero pygmalion scheherazade tantalus vitruvius vulcan writer'.split())
    assert {id for row in rows for id in row['hoards']}==expected
    assert len({row['name'] for row in rows})==50
    for row in rows:
        Coworker.model_validate(row)


def test_each_hoard_specialist_can_discover_mcp_without_bypassing_parent_permissions():
    from src import agent_defs
    from src.coworkers import specialist_templates
    from src.subagent_permissions import derive, ChildPermissions
    rows = specialist_templates()
    expected = {h['id'] for h in __import__('json').loads(
        (__import__('pathlib').Path(__file__).parents[1] / 'docs/HOARDS_INVENTARIO_2026-10-04.json').read_text(encoding='utf-8'))['hoards']}
    assert len(rows) == 38
    assert {r['hoards'][0] for r in rows} == expected
    for row in rows:
        definition = agent_defs.get(row['agent'])
        assert definition is not None
        permissions = derive(None, definition)
        assert not permissions.tool_denied('lookup_tools')
        assert not permissions.tool_denied('mcp__connected__crm_list')
        assert permissions.tool_denied('bash')
        assert not permissions.may_delegate
        parent = ChildPermissions(allowed_tools=frozenset({'read_file'}), may_delegate=True)
        restricted = derive(parent, definition)
        assert restricted.tool_denied('mcp__connected__crm_list')


def test_agency_source_revision_and_methods_are_traceable():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parents[1] / 'config/agents/agency/specialists.json').read_text(encoding='utf-8'))
    assert len(data['sources']) == 12
    for row in data['specialists']:
        source = data['sources'][row['method']]
        assert data['revision'] in row['source']
        assert len(source['sha256']) == 64
