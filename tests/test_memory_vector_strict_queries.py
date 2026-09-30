"""Strict vector retrieval preserves failures without changing legacy queries."""
import pytest
from src.memory_vector import MemoryVectorStore
from src.embedding_lanes import EmbeddingLane


class Encoder:
    def encode(self, texts, normalize_embeddings=True):
        return [[1., 0., 0.] for _ in texts]


class Collection:
    def __init__(self, failure=None, count=1, ids=None, distances=None):
        self.failure, self.value = failure, count
        self.ids, self.distances = ids or ['qa-id'], distances or [0.]
        self.count_calls = 0
        self.query_calls = []
    def count(self):
        self.count_calls += 1
        if self.failure == 'count': raise OSError('QA count failure')
        return self.value
    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.failure == 'query': raise OSError('QA query failure')
        return {'ids': [self.ids], 'distances': [self.distances]}


def vector(*collections):
    obj = MemoryVectorStore.__new__(MemoryVectorStore)
    obj._healthy = True
    obj._lanes = [EmbeddingLane('custom' if i == 0 else 'fastembed', Encoder(), collection,
        'qa-'+str(i), 'qa-3d', '', 3, 'qa') for i, collection in enumerate(collections)]
    return obj


def test_legacy_count_failure_is_still_empty():
    assert vector(Collection(failure='count')).search('qa') == []


def test_legacy_partial_failure_is_still_partial():
    assert vector(Collection(), Collection(failure='query')).search('qa') == [
        {'memory_id': 'qa-id', 'score': 1., 'embedding_lane': 'custom'}]


@pytest.mark.parametrize('failure', ['count', 'query'])
@pytest.mark.parametrize('bad_first', [False, True])
def test_any_lane_failure_raises(failure, bad_first):
    collections = [Collection(), Collection(failure=failure)]
    if bad_first: collections.reverse()
    with pytest.raises(OSError): vector(*collections).search('qa', strict=True)


@pytest.mark.parametrize('count', [None, -1, True, 1.0, '1'])
def test_invalid_count_is_not_known_empty(count):
    with pytest.raises(ValueError): vector(Collection(count=count)).search('qa', strict=True)


def test_zero_counts_once_without_query():
    collection = Collection(count=0)
    assert vector(collection).search('qa', strict=True) == []
    assert collection.count_calls == 1 and collection.query_calls == []


@pytest.mark.parametrize('limit', [True, 0, -1, 1.0, '1'])
def test_invalid_limit_does_not_certify_empty(limit):
    with pytest.raises(ValueError):
        vector(Collection(count=0)).search('qa', k=limit, strict=True)


def test_count_once_and_query_limit_preserved():
    collection = Collection(count=100)
    assert vector(collection).search('qa', k=3, strict=True)[0]['memory_id'] == 'qa-id'
    assert collection.count_calls == 1
    assert collection.query_calls[0]['n_results'] == 3
    assert collection.query_calls[0]['include'] == ['distances']


def test_priority_dedupe_and_cosine_score_match_legacy():
    obj = vector(Collection(count=2, ids=['shared', 'custom-only'], distances=[.12555, .5]),
                 Collection(count=2, ids=['shared', 'fallback-only'], distances=[.12555, .25]))
    rows = obj.search('qa', k=3, strict=True)
    assert rows == obj.search('qa', k=3)
    assert rows[0] == {'memory_id': 'shared', 'score': .8744, 'embedding_lane': 'custom'}
    assert [row['memory_id'] for row in rows] == ['shared', 'fallback-only', 'custom-only']


def test_malformed_partial_result_raises():
    with pytest.raises(ValueError):
        vector(Collection(), Collection(ids=['a','b'], distances=[0.])).search('qa', strict=True)


@pytest.mark.parametrize('condition', ['unhealthy', 'no_lanes', 'empty_query'])
def test_unavailable_queries_cannot_certify_absence(condition):
    obj = vector(Collection())
    if condition == 'unhealthy': obj._healthy = False
    if condition == 'no_lanes': obj._lanes = []
    with pytest.raises((RuntimeError, ValueError)):
        obj.search('' if condition == 'empty_query' else 'qa', strict=True)


def test_real_chroma_query_absence_and_partial_failure(tmp_path, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    try:
        from chromadb.is_thin_client import is_thin_client
    except ImportError:
        is_thin_client = False
    if is_thin_client:
        pytest.skip('requires isolated full Chroma QA runtime')
    client = chromadb.PersistentClient(path=str(tmp_path/'memory-vectors'),
        settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection('memory-vector-qa', embedding_function=None,
        metadata={'hnsw:space': 'cosine'})
    collection.add(ids=['near', 'far'], embeddings=[[1.,0.,0.],[0.,1.,0.]])
    obj = vector(collection)
    def forbidden(*a, **kw): pytest.fail('query initialized or reconnected vector store')
    monkeypatch.setattr(MemoryVectorStore, '_initialize', forbidden)
    monkeypatch.setattr(MemoryVectorStore, 'reconnect', forbidden)
    rows = obj.search('qa', k=2, strict=True)
    assert rows == obj.search('qa', k=2)
    assert [row['memory_id'] for row in rows] == ['near','far']
    with pytest.raises(OSError): vector(collection, Collection(failure='count')).search('qa', strict=True)
    with pytest.raises(OSError): vector(collection, Collection(failure='query')).search('qa', strict=True)
    collection.delete(ids=['near','far'])
    assert obj.search('qa', strict=True) == []
    client.delete_collection('memory-vector-qa')
    with pytest.raises(Exception): obj.search('qa', strict=True)
