"""Strict hybrid reads preserve legacy scoring without store construction."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import pytest
from src import memory_engine as engine, memory_conflicts
from src.memory_vector import MemoryVectorStore
from src.embedding_lanes import EmbeddingLane

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class Encoder:
    def __init__(self):
        self.url, self.model, self._dim = 'local://qa', 'qa-3d', 3
        self.api_key = None
        self._batch_size, self._max_chars = 8, 4000
    def encode(self, texts, normalize_embeddings=True): return [[1.,0.,0.] for _ in texts]


class Collection:
    def __init__(self, ids, failed=False):
        self.ids, self.failed = ids, failed
        self._client = object()
        self._model = SimpleNamespace(id='qa', name='qa', tenant='qa', database='qa')
        self.queries = []
    def count(self):
        if self.failed: raise OSError('QA collection count failure')
        return len(self.ids)
    def query(self, **kwargs):
        self.queries.append(kwargs)
        ids = self.ids[:kwargs['n_results']]
        return {'ids':[ids], 'distances':[[.2 for _ in ids]]}


class EngineVectors(MemoryVectorStore):
    pass


def vectors(collection):
    obj = EngineVectors.__new__(EngineVectors)
    obj._healthy = True
    obj._lanes = [EmbeddingLane('custom', Encoder(), collection, 'qa', 'qa-3d', 'local://qa', 3, 'qa')]
    obj._collection = collection
    return obj


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(engine, 'memory_conflict_detection_enabled', lambda: True)
    engine.set_vector_store(None)
    with engine._db(): pass
    with memory_conflicts._db(): pass
    yield tmp_path
    engine.reset_vector_store()


def add(text='Always run QA tests', **kwargs):
    return engine.add_item(text, owner=kwargs.pop('owner','qa'), project=kwargs.pop('project','workspace'),
        level='procedural', trust_class='human_explicit', now=NOW, **kwargs)


def forbid_writers(monkeypatch):
    def forbidden(*a, **kw): pytest.fail('strict query invoked writer or constructor')
    for name in ('_db','_open','_connect','_quarantine','vector_store','touch'):
        monkeypatch.setattr(engine, name, forbidden)
    monkeypatch.setattr(memory_conflicts, '_db', forbidden)
    monkeypatch.setattr(MemoryVectorStore, '__init__', forbidden)
    monkeypatch.setattr(MemoryVectorStore, '_initialize', forbidden)
    monkeypatch.setattr(MemoryVectorStore, 'reconnect', forbidden)


@pytest.mark.parametrize('semantic', [False, True])
def test_actual_sql_parity_and_no_writers(database, monkeypatch, semantic):
    row = add(evidence=[{'ref':'src/cart.py'}])
    collection = Collection([row['id']])
    engine.set_vector_store(vectors(collection))
    legacy = engine.search('src/cart.py QA tests', owner='qa', project='workspace', now=NOW,
                           touch_hits=False, semantic_enabled=semantic)
    with sqlite3.connect(engine.db_path()) as conn: conn.execute('PRAGMA journal_mode=DELETE')
    before = Path(engine.db_path()).read_bytes()
    forbid_writers(monkeypatch)
    strict = engine.context_search('src/cart.py QA tests', 'qa', 'workspace', now=NOW,
                                   semantic_enabled=semantic)
    assert strict == legacy
    assert strict[0]['graph'] > 0 and strict[0]['access_count'] == 0
    assert Path(engine.db_path()).read_bytes() == before


def test_conflict_marker_does_not_change_raw_bm25(database, monkeypatch):
    row = add('Always run QA tests')
    with sqlite3.connect(engine.db_path()) as conn:
        conn.execute("INSERT INTO memory_conflicts(id,owner,new_id,old_id,status) VALUES(?,?,?,?,?)",
                     ('qa-conflict','qa','new',row['id'],'open'))
    engine.set_vector_store(vectors(Collection([row['id']])))
    query = 'contradicted newer memory'
    legacy = engine.search(query, owner='qa', project='workspace', now=NOW, touch_hits=False)
    forbid_writers(monkeypatch)
    strict = engine.context_search(query, 'qa', 'workspace', now=NOW)
    assert strict == legacy and strict[0]['lexical'] == 0
    assert strict[0]['semantic'] == .8 and strict[0]['relevance'] == .36
    assert 'contradicted by a newer memory' in strict[0]['text']


def test_snapshot_default_and_raw_public_pairs(database):
    row = add()
    default = engine.context_snapshot('qa','workspace',now=NOW)
    pairs = engine.context_snapshot('qa','workspace',now=NOW,include_items=True)
    assert pairs[0][0]['id'] == row['id'] and pairs[0][1] == default[0]


def test_known_empty_sql_does_not_resolve_vector_store(database, monkeypatch):
    forbid_writers(monkeypatch)
    assert engine.context_search('QA query', 'qa','workspace') == []


@pytest.mark.parametrize('state', ['not_tried','none','unhealthy','unknown'])
def test_nonempty_scope_requires_known_installed_store(database, monkeypatch, state):
    row = add()
    if state == 'not_tried': engine.reset_vector_store()
    elif state == 'none': engine.set_vector_store(None)
    elif state == 'unhealthy':
        obj=vectors(Collection([row['id']]));obj._healthy=False;engine.set_vector_store(obj)
    else: engine.set_vector_store(SimpleNamespace(healthy=True,search=lambda *a,**k:[]))
    forbid_writers(monkeypatch)
    with pytest.raises(engine.MemoryEngineError): engine.context_search('QA tests','qa','workspace')


def test_semantic_disabled_needs_no_store_and_is_not_degraded(database, monkeypatch):
    add()
    engine.reset_vector_store()
    forbid_writers(monkeypatch)
    monkeypatch.setattr(engine,'_context_semantic_scores',lambda *a:pytest.fail('disabled semantic call'))
    rows=engine.context_search('QA tests','qa','workspace',semantic_enabled=False)
    assert rows and rows[0]['semantic']==0 and not rows[0]['degraded']


def test_partial_lane_error_raises(database):
    row=add()
    obj=vectors(Collection([row['id']]))
    obj._lanes.append(EmbeddingLane('fastembed',Encoder(),Collection([],failed=True),'bad','qa','',3,'bad'))
    engine.set_vector_store(obj)
    with pytest.raises(OSError): engine.context_search('QA tests','qa','workspace')


@pytest.mark.parametrize('change', ['binding','url','model','key','client','collection','namespace','path'])
def test_runtime_drift_discards_query(database, change):
    row=add()
    collection=Collection([row['id']]);obj=vectors(collection)
    engine.set_vector_store(obj)
    query=collection.query
    def mutate(**kwargs):
        result=query(**kwargs)
        lane=obj._lanes[0]
        if change=='binding':engine.set_vector_store(vectors(Collection([row['id']])))
        elif change=='url':lane.client.url='changed'
        elif change=='model':lane.client.model='changed'
        elif change=='key':lane.client.api_key='QA-only-changed'
        elif change=='client':lane.client=Encoder()
        elif change=='collection':lane.collection=Collection([row['id']])
        elif change=='namespace':collection._model.tenant='changed'
        else:engine.DATA_DIR=str(database/'other-path')
        return result
    collection.query=mutate
    with pytest.raises(engine.MemoryEngineError): engine.context_search('QA tests','qa','workspace')


def test_vector_ids_outside_sql_scope_are_never_returned(database):
    visible=add('Visible QA tests')
    hidden=add('Hidden QA tests',owner='other')
    engine.set_vector_store(vectors(Collection([hidden['id'],visible['id']])))
    rows=engine.context_search('QA tests','qa','workspace')
    assert [row['id'] for row in rows]==[visible['id']]


def test_runtime_change_during_sql_snapshot_rejects_before_encoding(database, monkeypatch):
    row=add()
    collection=Collection([row['id']]);obj=vectors(collection)
    engine.set_vector_store(obj)
    original=engine.context_snapshot
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        obj._lanes[0].client.url='changed-during-snapshot'
        return result
    monkeypatch.setattr(engine,'context_snapshot',changed)
    with pytest.raises(engine.MemoryEngineError):engine.context_search('QA tests','qa','workspace')
    assert collection.queries==[]


def test_runtime_identity_retains_replaced_model_object():
    import gc
    import weakref
    from src.embedding_runtime_identity import vector_identity
    class Model: pass
    obj=vectors(Collection(['qa']))
    obj._lanes[0].client._model=Model()
    reference=weakref.ref(obj._lanes[0].client._model)
    identity=vector_identity(obj)
    del obj._lanes[0].client._model
    gc.collect()
    assert reference() is not None
    del identity
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize('kind',['missing','corrupt','schema'])
def test_unknown_sql_store_is_never_repaired(database, monkeypatch, kind):
    path=Path(engine.db_path())
    if kind=='missing':path.unlink()
    elif kind=='corrupt':path.write_bytes(b'QA corrupt SQLite')
    else:
        with sqlite3.connect(path) as conn:conn.execute('DROP TABLE memory_conflicts')
    before=path.read_bytes() if path.exists() else None
    forbid_writers(monkeypatch)
    with pytest.raises(sqlite3.Error):engine.context_search('QA tests','qa','workspace')
    assert (path.read_bytes() if path.exists() else None)==before
    assert not list(database.glob('*.corrupt*'))


def test_real_chroma_and_sqlite_read_only_hybrid(database, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    try:
        from chromadb.is_thin_client import is_thin_client
    except ImportError:is_thin_client=False
    if is_thin_client:pytest.skip('requires isolated full Chroma QA runtime')
    row=add()
    client=chromadb.PersistentClient(path=str(database/'chroma'),settings=Settings(anonymized_telemetry=False))
    collection=client.create_collection('strict-engine-qa',embedding_function=None,metadata={'hnsw:space':'cosine'})
    collection.add(ids=[row['id']],embeddings=[[1.,0.,0.]])
    obj=vectors(collection);engine.set_vector_store(obj)
    legacy=engine.search('QA tests',owner='qa',project='workspace',now=NOW,touch_hits=False)
    forbid_writers(monkeypatch)
    rows=engine.context_search('QA tests','qa','workspace',now=NOW)
    assert rows==legacy and rows[0]['semantic']==1 and rows[0]['access_count']==0
    with pytest.raises(OSError):
        obj._lanes.append(EmbeddingLane('bad',Encoder(),Collection([],failed=True),'bad','qa','',3,'bad'))
        engine.context_search('QA tests','qa','workspace',now=NOW)
    obj._lanes.pop()
    collection.delete(ids=[row['id']])
    # A known empty vector index does not erase the owned SQL lexical candidate.
    rows=engine.context_search('QA tests','qa','workspace',now=NOW)
    assert rows[0]['semantic']==0 and not rows[0]['degraded']
    client.delete_collection('strict-engine-qa')
    with pytest.raises(Exception):engine.context_search('QA tests','qa','workspace')
