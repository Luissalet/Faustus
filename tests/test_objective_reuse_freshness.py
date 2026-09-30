"""Objectives freshness uses real JSONL queries and real compiler SQLite ledger."""
import asyncio
import json
from dataclasses import replace

import pytest
from services import objectives
from src.context_engine import wiring, compiler, store
from src.context_engine.cache import WorkingSet
from src.context_engine.adapters.objectives import ObjectivesSource
from src.context_engine.adapters.files import FileSource
from src.context_engine.contracts import ContextRequest, ContextExecution, ContextTask, ContextPolicy


@pytest.fixture
def live(tmp_path, monkeypatch):
    (tmp_path/'context.txt').write_text('Stable baseline context for the current implementation.',encoding='utf-8')
    project={'workspace':str(tmp_path)}
    request=ContextRequest(request_id='objectives-live',execution=ContextExecution(owner='fixture',workspace=str(tmp_path)),
        task=ContextTask(query='Read file context.txt and implement the project objectives'),
        explicit_refs=('file:context.txt',),policy=ContextPolicy(max_items_per_source=2))
    store.use_path(str(tmp_path/'ledger.sqlite3'))
    engine=compiler.ContextCompiler(sources=[ObjectivesSource(),FileSource()],cache=WorkingSet())
    monkeypatch.setattr(compiler,'compiler',lambda:engine)
    monkeypatch.setattr(wiring,'enabled',lambda:True)
    monkeypatch.setattr(wiring,'_live_budget',lambda *a,**k:6000)
    monkeypatch.setattr(wiring,'_remember_omitted',lambda *a:[])
    yield project, request, engine
    store.use_path(None)


async def deliver(request,previous=None):
    return await wiring.deliver_round(request=request,messages=[],previous=previous)


def add(project,title,priority=3):
    result=objectives.apply_deltas(project,[{'op':'ADD','title':title,'priority':priority}],actor='fixture')
    assert not result['conflicts']
    return next(r['id'] for r in result['state']['objectives'] if r['title']==title)


@pytest.mark.asyncio
async def test_real_unchanged_query_reuses_and_receipt_is_private(live):
    project,request,_=live
    add(project,'Deliver current goal')
    first=await deliver(request); second=await deliver(request,first)
    assert first is not None and 'Deliver current goal' in str(first['message'])
    assert second['report']['reused'] is True and second['message'] is first['message']
    assert first['_objective_reuse_receipts']
    assert '_objective_reuse_receipts' not in json.dumps(first['message'])+json.dumps(first['report'])
    assert second['_objective_reuse_receipts'] is first['_objective_reuse_receipts']
    with store.db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_packets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_new_goal_after_observed_absence_recompiles(live):
    project,request,_=live
    first=await deliver(request)
    assert first is not None and first['_objective_reuse_receipts']
    before=json.dumps(first['message'],sort_keys=True)
    add(project,'NEW goal from absent collection',priority=1)
    second=await deliver(request,first)
    assert not second['report'].get('reused')
    assert 'NEW goal from absent collection' in str(second['message'])
    assert json.dumps(first['message'],sort_keys=True)==before


@pytest.mark.asyncio
async def test_title_change_same_timestamp_and_status_transition(live):
    project,request,_=live
    oid=add(project,'Old title')
    first=await deliver(request)
    state=objectives.load_state(project,strict=True)
    state['objectives'][oid]['title']='Changed without clock movement'
    objectives.save_state(project,state)
    second=await deliver(request,first)
    assert not second['report'].get('reused') and 'Changed without clock movement' in str(second['message'])
    state['objectives'][oid].update(status='done',notes='Settled decision')
    objectives.save_state(project,state)
    third=await deliver(request,second)
    assert not third['report'].get('reused') and 'Settled decision' in str(third['message'])


@pytest.mark.asyncio
async def test_membership_and_top_query_are_replayed_without_widening(live):
    project,request,_=live
    add(project,'Priority one',1); add(project,'Priority two',2)
    first=await deliver(request)
    receipt=first['_objective_reuse_receipts'][0]
    assert receipt.retrieval.top()==2
    add(project,'Outside captured top',4)
    second=await deliver(request,first)
    assert second['report']['reused'] is True
    add(project,'New priority one entrant',1)
    third=await deliver(request,second)
    assert not third['report'].get('reused') and 'New priority one entrant' in str(third['message'])
    assert 'Outside captured top' not in str(third['message'])


@pytest.mark.asyncio
async def test_corrupt_store_is_unknown_not_empty_and_original_preserved(live):
    project,request,_=live
    add(project,'Previously observed goal')
    first=await deliver(request)
    path=objectives.objectives_path(project)
    with open(path,'wb') as f:f.write(b'not-json\n')
    second=await deliver(request,first)
    assert not second['report'].get('reused')
    assert second['_objective_reuse_receipts'] is None
    assert 'Previously observed goal' in str(first['message'])
    with open(path,'rb') as f:assert f.read()==b'not-json\n'
    objectives.save_state(project,{'objectives':{},'edges':[]})
    third=await deliver(request,second)
    assert not third['report'].get('reused') and third['_objective_reuse_receipts']


@pytest.mark.asyncio
async def test_unknown_legacy_and_mutated_scope_receipts_do_not_certify(live):
    project,request,_=live
    add(project,'Scoped goal')
    first=await deliver(request)
    legacy={k:v for k,v in first.items() if k!='_objective_reuse_receipts'}
    assert not (await deliver(request,legacy))['report'].get('reused')
    receipt=first['_objective_reuse_receipts'][0]
    other_request=replace(receipt.retrieval.request,execution=replace(request.execution,owner='another'))
    forged=dict(first,_objective_reuse_receipts=(replace(receipt,retrieval=replace(receipt.retrieval,request=other_request)),))
    assert not (await deliver(request,forged))['report'].get('reused')


@pytest.mark.asyncio
async def test_unavailable_source_and_recovery_are_not_reusable(live,monkeypatch):
    project,request,_=live
    add(project,'Available goal')
    first=await deliver(request)
    with monkeypatch.context() as patch:
        patch.setattr(ObjectivesSource,'available',lambda self:False)
        second=await deliver(request,first)
        assert not second['report'].get('reused') and second['_objective_reuse_receipts'] is None
    third=await deliver(request,second)
    assert not third['report'].get('reused') and 'Available goal' in str(third['message'])


@pytest.mark.asyncio
async def test_concurrent_consumers_keep_their_own_query_receipts(live,tmp_path):
    project,request,_=live
    second_workspace=tmp_path/'second';second_workspace.mkdir()
    (second_workspace/'context.txt').write_text('Second baseline',encoding='utf-8')
    other_project={'workspace':str(second_workspace)}
    add(project,'First project goal');add(other_project,'Second project goal')
    other_request=replace(request,execution=replace(request.execution,workspace=str(second_workspace)))
    first,other=await asyncio.gather(deliver(request),deliver(other_request))
    add(other_project,'Changed second project',1)
    stable,changed=await asyncio.gather(deliver(request,first),deliver(other_request,other))
    assert stable['report']['reused'] is True
    assert not changed['report'].get('reused')
    assert 'Changed second project' not in str(stable['message'])




@pytest.mark.asyncio
async def test_removed_goal_invalidates_query_membership(live):
    project,request,_=live
    oid=add(project,'Goal to remove')
    first=await deliver(request)
    state=objectives.load_state(project,strict=True); del state['objectives'][oid]
    objectives.save_state(project,state)
    second=await deliver(request,first)
    assert not second['report'].get('reused') and 'Goal to remove' not in str(second['message'])


@pytest.mark.asyncio
async def test_policy_does_not_read_objectives(live,monkeypatch):
    _,request,_=live
    request=replace(request,policy=replace(request.policy,allow_project_sources=False))
    monkeypatch.setattr(objectives,'load_state',lambda *a,**k:pytest.fail('policy forbids objective store read'))
    first=await deliver(request);second=await deliver(request,first)
    # FileSource also respects project-source policy, so no body is delivered.
    assert first is None and second is None


@pytest.mark.asyncio
async def test_read_error_after_valid_snapshot_is_unknown(live,monkeypatch):
    project,request,_=live
    add(project,'Observed goal')
    first=await deliver(request)
    def fail(*a,**k):raise OSError('synthetic read failure')
    monkeypatch.setattr(objectives,'load_state',fail)
    second=await deliver(request,first)
    assert not second['report'].get('reused') and second['_objective_reuse_receipts'] is None
    assert 'Observed goal' in str(first['message'])


@pytest.mark.asyncio
async def test_validation_timeout_is_unknown_and_compilation_keeps_own_deadline(live,monkeypatch):
    project,request,_=live
    add(project,'Observed goal')
    first=await deliver(request)
    original=ObjectivesSource.search;calls=[]
    async def search(self,req):
        calls.append(req)
        if len(calls)==1:await asyncio.sleep(0.1)
        return await original(self,req)
    monkeypatch.setattr(ObjectivesSource,'search',search)
    monkeypatch.setattr(wiring,'timeout_s',lambda:0.04)
    second=await deliver(request,first)
    assert second is not None and not second['report'].get('reused')
    assert len(calls)==2
    assert calls[0].sections==calls[1].sections and calls[0].lanes==calls[1].lanes


@pytest.mark.asyncio
async def test_receipt_bound_and_failure_before_query_are_not_absence(live,monkeypatch):
    project,request,_=live
    add(project,'Observed goal')
    first=await deliver(request)
    forged=dict(first,_objective_reuse_receipts=first['_objective_reuse_receipts']*17)
    assert not (await deliver(request,forged))['report'].get('reused')
    with monkeypatch.context() as patch:
        patch.setattr(compiler.ContextCompiler,'_build_retrieval_plan',lambda *a:(_ for _ in ()).throw(RuntimeError('before query')))
        # Failing compilation with no content may yield None, but never a reusable receipt.
        degraded=await deliver(request)
        assert degraded is None or degraded['_objective_reuse_receipts'] is None


def test_objective_body_without_query_backing_is_unknown():
    from src.context_engine.objective_reuse import captured
    from src.context_engine.contracts import ContextPacket,ContextSection,ContextItem
    packet=ContextPacket(packet_id='synthetic',sections=(ContextSection(kind='active_goal',items=(
        ContextItem(item_id='objective',source_type='objective',source_ref='objective:OBJ-1',body='Manual goal'),)),))
    assert captured({'seen':True,'receipts':[]},packet) is None


def test_context_capture_resets_after_error_and_does_not_retain_unbounded_queries():
    from src.context_engine.objective_reuse import capture_queries,captured,record_query,capturing,MAX_QUERIES
    from src.context_engine.candidates import RetrievalRequest,SourceResult
    request=RetrievalRequest(request=ContextRequest(request_id='synthetic'))
    with pytest.raises(RuntimeError):
        with capture_queries() as state:
            for _ in range(MAX_QUERIES+100):
                record_query(request,[SourceResult(source_id='objectives')],True)
            assert len(state['receipts'])==MAX_QUERIES+1
            assert captured(state) is None
            raise RuntimeError('synthetic failure')
    assert capturing() is False
