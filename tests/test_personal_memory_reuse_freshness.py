"""Personal-memory query receipts use the real owner-scoped JSON store and compiler."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from src.memory import MemoryManager, MemoryStoreUnreadable
from src import constants
from src.context_engine import compiler, wiring, store
from src.context_engine.cache import WorkingSet
from src.context_engine.adapters.memory import PersonalMemorySource
from src.context_engine.adapters.files import FileSource
from src.context_engine.contracts import ContextRequest,ContextExecution,ContextTask,ContextPolicy,ContextCandidate


@pytest.fixture
def live(tmp_path,monkeypatch):
    data=tmp_path/'memory-data';data.mkdir()
    manager=MemoryManager(str(data))
    monkeypatch.setattr(constants,'DATA_DIR',str(data))
    (tmp_path/'context.txt').write_text('Stable context baseline.',encoding='utf-8')
    request=ContextRequest(request_id='personal-query',execution=ContextExecution(owner='fixture',workspace=str(tmp_path)),
        task=ContextTask(query='I prefer Python tools'),
        policy=ContextPolicy(max_items_per_source=2))
    store.use_path(str(tmp_path/'ledger.sqlite3'))
    engine=compiler.ContextCompiler(sources=[PersonalMemorySource(manager=manager)],cache=WorkingSet())
    real_compile=engine.compile
    async def compile_with_baseline(request,**kwargs):
        # A mandatory fixture baseline keeps the packet nonempty for absent memory.
        # The actual query, adapter, transformations, receipts and SQLite ledger remain real.
        baseline=ContextCandidate(candidate_id='baseline',source_type='memory',source_ref='fixture:baseline',
            section='project_rules',body='Stable mandatory fixture baseline',trust_class='human_explicit',authority='binding_decision')
        return await real_compile(request,mandatory=(baseline,),**kwargs)
    monkeypatch.setattr(engine,'compile',compile_with_baseline)
    monkeypatch.setattr(compiler,'compiler',lambda:engine)
    monkeypatch.setattr(wiring,'enabled',lambda:True)
    monkeypatch.setattr(wiring,'_live_budget',lambda *a,**k:6000)
    monkeypatch.setattr(wiring,'_remember_omitted',lambda *a:[])
    yield manager,request,engine
    store.use_path(None)


async def deliver(request,previous=None):
    return await wiring.deliver_round(request=request,messages=[],previous=previous)


def add(manager,text='I prefer Python tools',owner='fixture'):
    entry=manager.add_entry(text,source='user',category='preference',owner=owner)
    manager.save(manager.load_all_for_update()+[entry])
    return entry


@pytest.mark.asyncio
async def test_unchanged_reuses_bytes_without_writes_or_uses(live):
    manager,request,_=live
    add(manager)
    first=await deliver(request)
    before=Path(manager.memory_file).read_bytes()
    second=await deliver(request,first)
    assert second['report']['reused'] is True and second['message'] is first['message']
    assert Path(manager.memory_file).read_bytes()==before
    assert manager.load('fixture')[0]['uses']==0
    assert first['_personal_memory_reuse_receipts']
    assert '_personal_memory_reuse_receipts' not in json.dumps(first['report'])+json.dumps(first['message'])
    with store.db() as connection:assert connection.execute('SELECT COUNT(*) FROM context_packets').fetchone()[0]==1


@pytest.mark.asyncio
async def test_empty_query_snapshot_discovers_new_memory(live):
    manager,request,_=live
    first=await deliver(request)
    assert first['_personal_memory_reuse_receipts']
    add(manager,'I prefer Python tools new preference')
    second=await deliver(request,first)
    assert not second['report'].get('reused') and 'new preference' in str(second['message'])


@pytest.mark.asyncio
async def test_changed_body_same_id_timestamp_and_deleted_membership(live):
    manager,request,_=live
    row=add(manager)
    first=await deliver(request)
    rows=manager.load('fixture');rows[0]['text']='I prefer Rust tools';manager.save(rows)
    second=await deliver(request,first)
    assert not second['report'].get('reused') and 'Rust tools' in str(second['message'])
    assert manager.load('fixture')[0]['id']==row['id'] and manager.load('fixture')[0]['timestamp']==row['timestamp']
    manager.save([])
    third=await deliver(request,second)
    assert not third['report'].get('reused') and 'Rust tools' not in str(third['message'])


@pytest.mark.asyncio
async def test_owner_isolation_without_admin_identity(live):
    manager,request,_=live
    add(manager,'I prefer Python tools owner-visible')
    add(manager,'I prefer Python tools hidden-other-owner',owner='other')
    first=await deliver(request)
    assert 'owner-visible' in str(first['message']) and 'hidden-other-owner' not in str(first['message'])
    rows=manager.load_all();next(r for r in rows if r['owner']=='other')['text']='I prefer Python tools changed-secret';manager.save(rows)
    second=await deliver(request,first)
    assert second['report']['reused'] is True and 'changed-secret' not in str(second['message'])
    assert manager.load('fixture')[0]['uses']==0


@pytest.mark.asyncio
async def test_incognito_gates_before_any_store_access(live,monkeypatch):
    _,request,_=live
    request=replace(request,policy=replace(request.policy,allow_personal_memory=False))
    monkeypatch.setattr(PersonalMemorySource,'_store',lambda self:pytest.fail('Incognito must not open personal store'))
    first=await deliver(request);second=await deliver(request,first)
    assert first is not None and second['report']['reused'] is True


@pytest.mark.asyncio
async def test_unavailable_source_is_unknown_then_recovers(live,monkeypatch):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    with monkeypatch.context() as patch:
        patch.setattr(PersonalMemorySource,'available',lambda self:False)
        patch.setattr(PersonalMemorySource,'_store',lambda self:pytest.fail('Unavailable source must not open store'))
        second=await deliver(request,first)
        assert not second['report'].get('reused') and second['_personal_memory_reuse_receipts'] is None
    third=await deliver(request,second)
    assert not third['report'].get('reused') and 'Python tools' in str(third['message'])


@pytest.mark.asyncio
async def test_corrupt_json_remains_unknown_and_bytes_preserved(live):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    path=Path(manager.memory_file);path.write_bytes(b'{corrupt-json')
    assert await wiring._personal_memory_reuse_is_current(request,first) is False
    assert path.read_bytes()==b'{corrupt-json'
    second=await deliver(request,first)
    assert second['_personal_memory_reuse_receipts'] is None
    assert not second['report'].get('reused') and path.read_bytes()==b'{corrupt-json'
    assert 'Python tools' in str(first['message'])


@pytest.mark.asyncio
async def test_missing_store_validation_never_creates_file_or_parent(live):
    manager,request,_=live
    first=await deliver(request) # Genuine empty query.
    path=Path(manager.memory_file);path.unlink();path.parent.rmdir()
    assert await wiring._personal_memory_reuse_is_current(request,first) is True
    assert not path.exists() and not path.parent.exists()


def test_readonly_loader_never_migrates_legacy_or_writes(tmp_path):
    path=tmp_path/'memory.json';path.write_bytes(b'broken-json')
    legacy=tmp_path/'memory.txt';legacy.write_text('Valid legacy sentence',encoding='utf-8')
    manager=MemoryManager(str(tmp_path),create_if_missing=False)
    with pytest.raises(MemoryStoreUnreadable):manager.load_context_snapshot('fixture')
    assert path.read_bytes()==b'broken-json' and legacy.read_text()=='Valid legacy sentence'
    assert not (tmp_path/'memory.json.tmp').exists()
    # Existing legacy reader still performs its historical conversion.
    assert manager.load_all()[0]['text']=='Valid legacy sentence'


@pytest.mark.asyncio
async def test_valid_old_json_projection_is_rendered_without_rewriting(live):
    manager,request,_=live
    old=[{'id':'legacy-owned','owner':'fixture','text':'I prefer Python tools legacy-format','timestamp':12345}]
    path=Path(manager.memory_file);path.write_text(json.dumps(old),encoding='utf-8');before=path.read_bytes()
    first=await deliver(request);second=await deliver(request,first)
    assert 'legacy-format' in str(first['message']) and second['report']['reused'] is True
    assert path.read_bytes()==before


@pytest.mark.asyncio
async def test_changed_query_and_private_legacy_backing_recompile(live):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    legacy={k:v for k,v in first.items() if k!='_personal_memory_reuse_receipts'}
    assert not (await deliver(request,legacy))['report'].get('reused')
    changed=replace(request,task=replace(request.task,query='Read file context.txt and prefer Rust tools'))
    assert not (await deliver(changed,first))['report'].get('reused')


@pytest.mark.asyncio
async def test_query_top_is_preserved_and_nonmatching_addition_does_not_widen(live):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    receipt=first['_personal_memory_reuse_receipts'][0]
    assert receipt.retrieval.top()==2
    add(manager,'Astronomy unrelated distant stars')
    second=await deliver(request,first)
    assert second['report']['reused'] is True
    assert 'Astronomy' not in str(second['message'])


@pytest.mark.asyncio
async def test_read_error_and_validation_timeout_do_not_certify(live,monkeypatch):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    with monkeypatch.context() as patch:
        patch.setattr(MemoryManager,'load_context_snapshot',lambda *a:(_ for _ in ()).throw(OSError('synthetic failure')))
        assert await wiring._personal_memory_reuse_is_current(request,first) is False
    original=PersonalMemorySource.search
    async def slow(self,req):
        await asyncio.sleep(0.1)
        return await original(self,req)
    monkeypatch.setattr(PersonalMemorySource,'search',slow);monkeypatch.setattr(wiring,'timeout_s',lambda:0.02)
    assert await wiring._personal_memory_reuse_is_current(request,first) is False
    assert manager.load('fixture')[0]['uses']==0


@pytest.mark.asyncio
async def test_concurrent_owner_queries_keep_receipts_independent(live):
    manager,request,_=live
    add(manager,'I prefer Python tools first-owner');add(manager,'I prefer Python tools second-owner',owner='other')
    other=replace(request,execution=replace(request.execution,owner='other'))
    first,second=await asyncio.gather(deliver(request),deliver(other))
    rows=manager.load_all();next(r for r in rows if r['owner']=='other')['text']='I prefer Rust tools changed-second-owner';manager.save(rows)
    stable,changed=await asyncio.gather(deliver(request,first),deliver(other,second))
    assert stable['report']['reused'] is True and not changed['report'].get('reused')
    assert 'changed-second-owner' not in str(stable['message'])


@pytest.mark.asyncio
async def test_initial_legacy_fallback_keeps_body_but_receipt_unknown(live,monkeypatch):
    manager,request,_=live;add(manager,'I prefer Python tools existing legacy-readable projection')
    # Existing owner-filtered rendering is real; only the strict observation fails.
    monkeypatch.setattr(MemoryManager,'load_context_snapshot',lambda *a:(_ for _ in ()).throw(MemoryStoreUnreadable('unknown snapshot')))
    first=await deliver(request)
    assert 'legacy-readable projection' in str(first['message'])
    assert first['_personal_memory_reuse_receipts'] is None
    before=Path(manager.memory_file).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(MemoryManager,'load',lambda *a:pytest.fail('Validation must not use legacy fallback'))
        assert await wiring._personal_memory_reuse_is_current(request,first) is False
    assert Path(manager.memory_file).read_bytes()==before


@pytest.mark.asyncio
async def test_unknown_initial_fallback_with_no_hits_is_not_observed_absence(live,monkeypatch):
    manager,request,_=live;add(manager,'Distant astronomy stars')
    monkeypatch.setattr(MemoryManager,'load_context_snapshot',lambda *a:(_ for _ in ()).throw(MemoryStoreUnreadable('unknown snapshot')))
    first=await deliver(request)
    assert first is not None and first['_personal_memory_reuse_receipts'] is None


@pytest.mark.asyncio
async def test_validation_missing_after_delivered_memory_never_recreates_store(live):
    manager,request,_=live;add(manager)
    first=await deliver(request)
    path=Path(manager.memory_file);path.unlink();path.parent.rmdir()
    assert await wiring._personal_memory_reuse_is_current(request,first) is False
    assert not path.exists() and not path.parent.exists()
