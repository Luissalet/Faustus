"""Actual document queries, compilation and SQLite ledger; no model downloads."""
from dataclasses import replace
import json
import pytest

from src.context_engine import compiler, wiring, store
from src.context_engine.cache import WorkingSet
from src.context_engine.adapters.documents import DocumentSource
from src.context_engine.contracts import ContextRequest, ContextExecution, ContextTask, ContextPolicy, ContextCandidate
from src.embedding_lanes import EmbeddingLane
from src.rag_vector import VectorRAG
from src.rag_manager import RAGManager


class Encoder:
    def __init__(self):
        self.url, self.model, self._dim = 'local://qa', 'qa-3d', 3
        self.calls = 0
    def encode(self, texts, normalize_embeddings=True):
        self.calls += 1
        return [[1., 0., 0.] for _ in texts]


class Collection:
    def __init__(self):
        self.rows = {}
        self.failed = False
    def count(self):
        if self.failed:
            raise OSError('QA collection failure')
        return len(self.rows)
    def query(self, **kw):
        rows = [(key, value) for key, value in self.rows.items()
                if value[1]['owner'] == kw['where']['owner']][:kw['n_results']]
        return {'ids': [[k for k, _ in rows]], 'documents': [[v[0] for _, v in rows]],
                'metadatas': [[v[1] for _, v in rows]], 'distances': [[0. for _ in rows]]}
    def get(self, **kw):
        return {'ids': list(self.rows), 'documents': [r[0] for r in self.rows.values()],
                'metadatas': [r[1] for r in self.rows.values()]}


def manager(collection):
    vector = VectorRAG.__new__(VectorRAG)
    vector._healthy = True
    vector._collection = collection
    vector._lanes = [EmbeddingLane('qa', Encoder(), collection, 'qa', 'qa-3d', 'local://qa', 3, 'qa')]
    obj = RAGManager.__new__(RAGManager)
    obj.vector_rag = vector
    return obj


@pytest.fixture
def live(tmp_path, monkeypatch):
    collection = Collection()
    runtime = manager(collection)
    source = DocumentSource(runtime)
    request = ContextRequest(request_id='document-query', execution=ContextExecution(owner='qa'),
        task=ContextTask(query='research hat documents', intent='research'),
        policy=ContextPolicy(max_items_per_source=2))
    store.use_path(str(tmp_path/'ledger.sqlite3'))
    engine = compiler.ContextCompiler(sources=[source], cache=WorkingSet())
    real_compile = engine.compile
    async def compile_baseline(request, **kwargs):
        baseline = ContextCandidate(candidate_id='baseline', source_type='memory', source_ref='fixture:baseline',
            section='project_rules', body='Stable mandatory baseline', trust_class='human_explicit', authority='binding_decision')
        return await real_compile(request, mandatory=(baseline,), **kwargs)
    monkeypatch.setattr(engine, 'compile', compile_baseline)
    monkeypatch.setattr(compiler, 'compiler', lambda: engine)
    monkeypatch.setattr(wiring, 'enabled', lambda: True)
    monkeypatch.setattr(wiring, '_live_budget', lambda *a, **kw: 6000)
    monkeypatch.setattr(wiring, '_remember_omitted', lambda *a: [])
    yield collection, runtime, request, source
    store.use_path(None)


async def deliver(request, previous=None):
    return await wiring.deliver_round(request=request, messages=[], previous=previous)


def add(collection, text='hat original', owner='qa', key='qa-id'):
    collection.rows[key] = (text, {'owner': owner, 'source': 'qa.txt', 'chunk_id': key})


async def test_unchanged_reuses_real_compiler_ledger(live, monkeypatch):
    collection, runtime, request, _ = live
    add(collection)
    first = await deliver(request)
    monkeypatch.setattr(RAGManager, '__init__', lambda *a, **k: pytest.fail('constructor during validation'))
    second = await deliver(request, first)
    assert first['_document_reuse_receipts']
    assert second['report']['reused'] and second['message'] is first['message']
    assert '_document_reuse_receipts' not in json.dumps(first['message'])+json.dumps(first['report'])
    with store.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM context_packets').fetchone()[0] == 1


async def test_absence_then_new_and_update_delete(live):
    collection, _, request, _ = live
    first = await deliver(request)
    assert first['_document_reuse_receipts']
    add(collection)
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'hat original' in str(second['message'])
    add(collection, 'hat changed')
    third = await deliver(request, second)
    assert not third['report'].get('reused') and 'hat changed' in str(third['message'])
    collection.rows.clear()
    fourth = await deliver(request, third)
    assert not fourth['report'].get('reused') and 'hat changed' not in str(fourth['message'])


async def test_other_owner_change_does_not_expand_query(live):
    collection, _, request, _ = live
    add(collection)
    first = await deliver(request)
    add(collection, 'hidden private content', 'other', 'other-id')
    second = await deliver(request, first)
    assert second['report']['reused'] and 'hidden private' not in str(second['message'])


async def test_top_membership_change_invalidates(live):
    collection, _, request, _ = live
    add(collection, key='first')
    add(collection, 'hat second', key='second')
    first = await deliver(request)
    old = dict(collection.rows)
    collection.rows.clear()
    add(collection, 'hat newly selected', key='new')
    collection.rows.update(old)
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'newly selected' in str(second['message'])


async def test_missing_private_receipt_and_changed_query_recompile(live):
    collection, _, request, _ = live
    add(collection)
    first = await deliver(request)
    legacy = {key: value for key, value in first.items() if key != '_document_reuse_receipts'}
    assert not (await deliver(request, legacy))['report'].get('reused')
    changed = replace(request, task=replace(request.task, query='research another hat'))
    assert not (await deliver(changed, first))['report'].get('reused')


async def test_partial_failure_and_missing_client_identity_unknown(live):
    collection, runtime, request, _ = live
    add(collection)
    first = await deliver(request)
    broken = Collection(); broken.failed = True
    runtime.vector_rag._lanes.append(EmbeddingLane('bad', Encoder(), broken, 'bad', 'qa', '', 3, 'bad'))
    assert not await wiring._document_reuse_is_current(request, first)
    second = await deliver(request, first)
    assert second['_document_reuse_receipts'] is None
    runtime.vector_rag._lanes.pop()
    del runtime.vector_rag._lanes[0].client._dim
    third = await deliver(request, second)
    assert third['_document_reuse_receipts'] is None
    assert 'hat original' in str(third['message'])


def test_unknown_manual_body_and_overflow_never_certified():
    from src.context_engine import document_reuse as reuse
    from types import SimpleNamespace
    manual = SimpleNamespace(sections=[SimpleNamespace(kind='retrieved_documents',
        items=[SimpleNamespace(source_ref='doc:manual#chunk0')])])
    with reuse.capture_queries() as state:
        reuse.record_query(None, (), False)
    assert reuse.captured(state, manual) is None
    with reuse.capture_queries() as state:
        for _ in range(40):
            reuse.record_query(None, (), True)
    assert len(state['receipts']) == reuse.MAX_QUERIES + 1
    assert reuse.captured(state) is None


@pytest.mark.parametrize('field', ['url', 'model', '_dim', 'api_key', '_client', '_max_chars', '_batch_size'])
async def test_live_client_drift_refuses_requery(live, field):
    collection, runtime, request, _ = live
    add(collection)
    first = await deliver(request)
    client = runtime.vector_rag._lanes[0].client
    before = client.calls
    setattr(client, field, 4 if field == '_dim' else 'changed')
    assert not await wiring._document_reuse_is_current(request, first)
    assert client.calls == before


@pytest.mark.parametrize('field', ['fingerprint', 'collection', 'client'])
async def test_lane_drift_is_unknown(live, field):
    collection, runtime, request, _ = live
    add(collection)
    first = await deliver(request)
    value = {'fingerprint': 'changed', 'collection': Collection(), 'client': Encoder()}[field]
    setattr(runtime.vector_rag._lanes[0], field, value)
    assert not await wiring._document_reuse_is_current(request, first)


async def test_worker_checks_expected_identity_before_encoding(live, monkeypatch):
    collection, runtime, request, _ = live
    add(collection)
    first = await deliver(request)
    client = runtime.vector_rag._lanes[0].client
    before = client.calls
    def change_before_search(source):
        client.url = 'changed-after-parent-validation'
        return runtime
    monkeypatch.setattr(DocumentSource, '_store', change_before_search)
    assert not await wiring._document_reuse_is_current(request, first)
    assert client.calls == before


@pytest.mark.parametrize('owner', ['qa', 'other-owner'])
async def test_original_source_manager_swap_invalidates(live, owner):
    collection, _, request, source = live
    add(collection, 'hat previous manager')
    first = await deliver(request)
    replacement = Collection()
    add(replacement, 'hat newly active manager', owner=owner)
    source._manager = manager(replacement)
    second = await deliver(request, first)
    assert not second['report'].get('reused')
    assert 'previous manager' not in str(second['message'])
    assert ('newly active manager' in str(second['message'])) == (owner == 'qa')


async def test_original_source_swap_in_worker_never_queries_old_manager(live, monkeypatch):
    collection, runtime, request, source = live
    add(collection)
    first = await deliver(request)
    client = runtime.vector_rag._lanes[0].client
    before = client.calls
    def swap_before_query(validation_source):
        source._manager = manager(Collection())
        return runtime
    monkeypatch.setattr(DocumentSource, '_store', swap_before_query)
    assert not await wiring._document_reuse_is_current(request, first)
    assert client.calls == before


async def test_original_source_swap_after_encode_discards_result(live, monkeypatch):
    collection, runtime, request, source = live
    add(collection)
    first = await deliver(request)
    client = runtime.vector_rag._lanes[0].client
    encode = client.encode
    def change_during_query(*args, **kwargs):
        source._manager = manager(Collection())
        return encode(*args, **kwargs)
    monkeypatch.setattr(client, 'encode', change_during_query)
    assert not await wiring._document_reuse_is_current(request, first)


async def test_strict_failure_preserves_legacy_render_but_unknown(live):
    collection, runtime, request, _ = live
    add(collection)
    runtime.search = lambda *a, **kw: (_ for _ in ()).throw(OSError('QA strict failure')) if kw.get('strict') else [
        {'id': 'qa-id', 'document': 'hat legacy fallback', 'similarity': 1.,
         'metadata': {'owner': 'qa', 'source': 'qa.txt'}}]
    first = await deliver(request)
    assert 'hat legacy fallback' in str(first['message'])
    assert first['_document_reuse_receipts'] is None
    second = await deliver(request, first)
    assert not second['report'].get('reused')


async def test_off_and_ownerless_gate(live, monkeypatch):
    _, _, request, _ = live
    monkeypatch.setattr(DocumentSource, '_store', lambda *a: pytest.fail('denied store open'))
    off = replace(request, policy=replace(request.policy, allow_personal_memory=False))
    first = await deliver(off)
    assert (await deliver(off, first))['report']['reused']
    ownerless = replace(request, execution=replace(request.execution, owner=''))
    result = await deliver(ownerless)
    assert result['_document_reuse_receipts'] is None


async def test_real_chroma_compiler_freshness(live, tmp_path):
    import chromadb
    from chromadb.config import Settings
    try:
        from chromadb.is_thin_client import is_thin_client
    except ImportError:
        is_thin_client = False
    if is_thin_client:
        pytest.skip('requires isolated full Chroma QA runtime')
    _, _, request, source = live
    client = chromadb.PersistentClient(path=str(tmp_path/'real-chroma'),
                                     settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection('freshness-qa', embedding_function=None,
                                          metadata={'hnsw:space': 'cosine'})
    source._manager = manager(collection)
    first = await deliver(request)
    assert first['_document_reuse_receipts']
    assert (await deliver(request, first))['report']['reused']
    collection.add(ids=['qa-id'], embeddings=[[1., 0., 0.]], documents=['hat new real document'],
                   metadatas=[{'owner': 'qa', 'source': 'qa.txt'}])
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'hat new real document' in str(second['message'])
    assert (await deliver(request, second))['report']['reused']
    collection.update(ids=['qa-id'], embeddings=[[1., 0., 0.]], documents=['hat updated real document'])
    third = await deliver(request, second)
    assert not third['report'].get('reused') and 'updated real' in str(third['message'])
    client.delete_collection('freshness-qa')
    assert not await wiring._document_reuse_is_current(request, third)
    with store.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM context_packets').fetchone()[0] == 3
