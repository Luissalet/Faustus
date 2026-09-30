"""Hybrid learned-memory receipts: strict context_search + packet reuse."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import pytest
from src import memory_engine as memory, memory_conflicts
from src.memory_vector import MemoryVectorStore
from src.embedding_lanes import EmbeddingLane
from src.context_engine import compiler, wiring, store
from src.context_engine import memory_engine_reuse as reuse
from src.context_engine.cache import WorkingSet
from src.context_engine.adapters import memory as adapter
from src.context_engine.adapters.memory import MemoryEngineSource
from src.context_engine.contracts import (ContextRequest, ContextExecution, ContextTask,
                                          ContextPolicy, ContextCandidate)


class Encoder:
    def __init__(self, model='qa-3d'):
        self.url, self.model, self._dim = 'local://qa', model, 3
        self.api_key = None
        self._batch_size, self._max_chars = 8, 4000
    def encode(self, texts, normalize_embeddings=True): return [[1., 0., 0.] for _ in texts]


class Collection:
    def __init__(self, ids):
        self.ids = list(ids)
        self._client = object()
        self._model = SimpleNamespace(id='qa', name='qa', tenant='qa', database='qa')
        self.queries = 0
    def count(self): return len(self.ids)
    def query(self, **kwargs):
        self.queries += 1
        ids = self.ids[:kwargs['n_results']]
        return {'ids': [ids], 'distances': [[.2 for _ in ids]]}


class EngineVectors(MemoryVectorStore):
    pass


def vectors(collection, model='qa-3d'):
    obj = EngineVectors.__new__(EngineVectors)
    obj._healthy = True
    obj._lanes = [EmbeddingLane('custom', Encoder(model), collection, 'qa', model, 'local://qa', 3, 'qa')]
    obj._collection = collection
    return obj


def install(collection, model='qa-3d'):
    runtime = vectors(collection, model)
    memory.set_vector_store(runtime)
    return runtime


@pytest.fixture
def live(tmp_path, monkeypatch):
    data = tmp_path/'memories'; data.mkdir()
    monkeypatch.setattr(memory, 'DATA_DIR', str(data))
    monkeypatch.setattr(memory, 'memory_conflict_detection_enabled', lambda: True)
    memory.set_vector_store(None)
    with memory._db(): pass
    with memory_conflicts._db(): pass
    request = ContextRequest(request_id='hybrid-query',
        execution=ContextExecution(owner='qa', workspace='workspace', project_id='p', session_id='qa-session'),
        task=ContextTask(query='Always QA tests', intent='research'),
        policy=ContextPolicy(max_items_per_source=3, allow_semantic_lane=False))
    store.use_path(str(tmp_path/'ledger.sqlite3'))
    engine = compiler.ContextCompiler(sources=[MemoryEngineSource()], cache=WorkingSet())
    real_compile = engine.compile
    async def compile_baseline(request, **kwargs):
        baseline = ContextCandidate(source_ref='fixture:baseline', source_type='memory', section='project_rules',
            body='Stable mandatory baseline', trust_class='human_explicit', authority='binding_decision')
        return await real_compile(request, mandatory=(baseline,), **kwargs)
    monkeypatch.setattr(engine, 'compile', compile_baseline)
    monkeypatch.setattr(compiler, 'compiler', lambda: engine)
    monkeypatch.setattr(wiring, 'enabled', lambda: True)
    monkeypatch.setattr(wiring, '_live_budget', lambda *a, **k: 6000)
    monkeypatch.setattr(wiring, '_remember_omitted', lambda *a: [])
    yield request, data
    memory.reset_vector_store()
    store.use_path(None)


def semantic_on(request):
    return replace(request, policy=replace(request.policy, allow_semantic_lane=True))


def add(text='Always run original QA tests', **kwargs):
    return memory.add_item(text, owner=kwargs.pop('owner', 'qa'), project=kwargs.pop('project', 'workspace'),
        level=kwargs.pop('level', 'procedural'), trust_class='human_explicit', **kwargs)


async def deliver(request, previous=None):
    return await wiring.deliver_round(request=request, messages=[], previous=previous)


def hybrid_receipts(result):
    receipts = result['_standing_memory_reuse_receipts']
    assert receipts and all(isinstance(r, reuse.HybridQueryReceipt) for r in receipts)
    return receipts


def forbid_writers(monkeypatch):
    def forbidden(*a, **kw): pytest.fail('validation invoked writer, legacy search or constructor')
    for name in ('_db', '_open', '_connect', '_quarantine', 'vector_store', 'touch', 'search'):
        monkeypatch.setattr(memory, name, forbidden)
    monkeypatch.setattr(memory_conflicts, '_db', forbidden)
    monkeypatch.setattr(MemoryVectorStore, '__init__', forbidden)


async def test_lexical_query_unchanged_reuses_without_writers(live, monkeypatch):
    request, _ = live
    add()
    first = await deliver(request)
    assert 'original QA' in str(first['message'])
    receipt = hybrid_receipts(first)[0]
    assert receipt.identity is None and receipt.clock.tzinfo is not None
    forbid_writers(monkeypatch)
    second = await deliver(request, first)
    assert second['report']['reused'] and second['message'] is first['message']
    with sqlite3.connect(memory.db_path()) as conn:
        assert conn.execute('SELECT access_count FROM items').fetchone()[0] == 0


async def test_body_update_and_new_matching_member_invalidate(live):
    request, _ = live
    row = add()
    first = await deliver(request)
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute('UPDATE items SET text=? WHERE id=?', ('Always run changed QA tests', row['id']))
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'changed QA' in str(second['message'])
    add('Always QA tests for the new member')
    third = await deliver(request, second)
    assert not third['report'].get('reused') and 'new member' in str(third['message'])


async def test_nonmatching_or_foreign_change_keeps_reuse(live):
    request, _ = live
    add()
    first = await deliver(request)
    add('Unrelated deployment note', level='semantic')
    add('Always QA tests hidden owner', owner='other')
    assert (await deliver(request, first))['report']['reused']


async def test_conflict_marker_invalidates(live):
    request, _ = live
    row = add()
    first = await deliver(request)
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute("INSERT INTO memory_conflicts(id,owner,new_id,old_id,status) VALUES(?,?,?,?,?)",
                     ('qa-conflict', 'qa', 'new', row['id'], 'open'))
    second = await deliver(request, first)
    assert not second['report'].get('reused')
    assert 'contradicted by a newer memory' in str(second['message'])


async def test_decay_alone_keeps_reuse_but_expiry_invalidates(live, monkeypatch):
    request, _ = live
    now = datetime.now(timezone.utc)
    add('Always QA tests decaying semantic memory', level='semantic')
    add('Always QA tests temporary rule', valid_until=now + timedelta(days=1))
    first = await deliver(request)
    assert 'temporary rule' in str(first['message'])
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return now + timedelta(days=2)
    monkeypatch.setattr(adapter, 'datetime', Later)
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'temporary rule' not in str(second['message'])
    assert 'decaying semantic' in str(second['message'])
    class EvenLater(datetime):
        @classmethod
        def now(cls, tz=None): return now + timedelta(days=9)
    monkeypatch.setattr(adapter, 'datetime', EvenLater)
    # Only time passed for the decaying member: the pinned scoring clock keeps it current.
    assert await wiring._standing_memory_reuse_is_current(request, second)


async def test_semantic_lane_binds_installed_runtime(live, monkeypatch):
    request, _ = live
    request = semantic_on(request)
    row = add()
    collection = Collection([row['id']])
    runtime = install(collection)
    first = await deliver(request)
    receipt = hybrid_receipts(first)[0]
    assert receipt.identity is not None and receipt.identity.references[0] is runtime
    assert collection.queries >= 1
    queries = collection.queries
    forbid_writers(monkeypatch)
    second = await deliver(request, first)
    assert second['report']['reused'] and collection.queries > queries


async def test_semantic_runtime_swap_or_removal_invalidates(live, monkeypatch):
    request, _ = live
    request = semantic_on(request)
    row = add()
    install(Collection([row['id']]))
    first = await deliver(request)
    hybrid_receipts(first)
    install(Collection([row['id']]), model='qa-other')
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    memory.set_vector_store(None)
    monkeypatch.setattr(memory, 'vector_store', lambda: pytest.fail('validation built vector store'))
    assert not await wiring._standing_memory_reuse_is_current(request, first)


async def test_semantic_hits_change_invalidates(live):
    request, _ = live
    request = semantic_on(request)
    row = add()
    other = add('Always QA tests second candidate')
    collection = Collection([row['id']])
    install(collection)
    first = await deliver(request)
    hybrid_receipts(first)
    collection.ids = [other['id']]
    assert not await wiring._standing_memory_reuse_is_current(request, first)


async def test_semantic_without_installed_store_stays_unknown(live, monkeypatch):
    request, _ = live
    request = semantic_on(request)
    add()
    first = await deliver(request)
    assert 'original QA' in str(first['message'])
    assert first['_standing_memory_reuse_receipts'] is None


async def test_strict_failure_falls_back_to_legacy_without_receipt(live, monkeypatch):
    request, _ = live
    add()
    monkeypatch.setattr(memory, 'context_search', lambda *a, **k: (_ for _ in ()).throw(OSError('strict failed')))
    first = await deliver(request)
    assert 'original QA' in str(first['message'])
    assert first['_standing_memory_reuse_receipts'] is None


@pytest.mark.parametrize('kind', ['missing', 'corrupt'])
async def test_validation_failure_never_repairs_store(live, kind):
    request, _ = live
    add()
    first = await deliver(request)
    hybrid_receipts(first)
    path = Path(memory.db_path())
    if kind == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'corrupt SQLite QA bytes')
    before = path.read_bytes() if path.exists() else None
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    assert (path.read_bytes() if path.exists() else None) == before
    assert not list(path.parent.glob('*.corrupt*'))


async def test_other_scope_cannot_reuse_receipt(live):
    request, _ = live
    add()
    first = await deliver(request)
    hybrid_receipts(first)
    other = replace(request, execution=replace(request.execution, owner='other'))
    assert not await wiring._standing_memory_reuse_is_current(other, first)


async def test_incognito_hybrid_query_no_db_access(live, monkeypatch):
    request, _ = live
    off = replace(request, policy=replace(request.policy, allow_personal_memory=False))
    monkeypatch.setattr(memory, 'context_search', lambda *a, **k: pytest.fail('off read DB'))
    monkeypatch.setattr(memory, '_db', lambda: pytest.fail('off initialized DB'))
    first = await deliver(off)
    assert first['_standing_memory_reuse_receipts'] == ()
    assert (await deliver(off, first))['report']['reused']


def test_record_rejects_runtime_drift_and_naive_clock():
    from src.context_engine.candidates import RetrievalRequest
    from src.context_engine.objective_reuse import projection_digest
    retrieval = RetrievalRequest(request=SimpleNamespace(), query='Always QA', lanes=('lexical',))
    result = SimpleNamespace(source_id='memory_engine', ok=lambda: True, candidates=())
    assert projection_digest(result)
    aware = datetime.now(timezone.utc)
    for bundle in (('a', 'a', 'x', 'y', aware, None), ('a', 'b', None, None, aware, None),
                   ('a', 'a', None, None, aware.replace(tzinfo=None), None)):
        with reuse.capture_queries() as state:
            reuse.completed(retrieval, bundle)
            reuse.record_query(retrieval, (result,), True)
        assert reuse.captured(state) is None
    with reuse.capture_queries() as state:
        reuse.completed(retrieval, ('a', 'a', None, None, aware, None))
        reuse.record_query(retrieval, (result,), True)
    assert isinstance(reuse.captured(state)[0], reuse.HybridQueryReceipt)


async def test_real_chroma_hybrid_receipt_reuse_and_invalidation(live, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    try:
        from chromadb.is_thin_client import is_thin_client
    except ImportError:
        is_thin_client = False
    if is_thin_client:
        pytest.skip('requires isolated full Chroma QA runtime')
    request, data = live
    request = semantic_on(request)
    row = add()
    client = chromadb.PersistentClient(path=str(data/'chroma'), settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection('hybrid-reuse-qa', embedding_function=None,
                                          metadata={'hnsw:space': 'cosine'})
    collection.add(ids=[row['id']], embeddings=[[1., 0., 0.]])
    install(collection)
    first = await deliver(request)
    receipt = hybrid_receipts(first)[0]
    assert receipt.identity is not None
    second = await deliver(request, first)
    assert second['report']['reused'] and second['message'] is first['message']
    collection.delete(ids=[row['id']])
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    client.delete_collection('hybrid-reuse-qa')
    assert not await wiring._standing_memory_reuse_is_current(request, first)
