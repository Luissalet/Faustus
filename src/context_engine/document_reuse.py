"""Private receipts for actual strict document queries; never serialized."""
import copy
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from .objective_reuse import projection_digest

_ACTIVE = ContextVar('document_query_capture', default=None)
MAX_QUERIES = 16


def _client_identity(client):
    # Inspect already materialized fields only; do not call dimension getters
    # or arbitrary properties that could initialize a model or do network I/O.
    values = vars(client)
    if any(key not in values for key in ('url', 'model', '_dim')):
        raise ValueError('embedding client query identity unavailable')
    if not isinstance(values['url'], str) or not isinstance(values['model'], str):
        raise ValueError('embedding client query identity malformed')
    dimension = values['_dim']
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError('embedding client dimension unavailable')
    secret = values.get('api_key')
    if secret is not None and not isinstance(secret, str):
        raise ValueError('embedding client credentials malformed')
    for key in ('_batch_size', '_max_chars'):
        if key in values and (isinstance(values[key], bool) or
                not isinstance(values[key], int) or values[key] <= 0):
            raise ValueError('embedding client query bounds malformed')
    return (hashlib.sha256(values['url'].encode()).hexdigest(), values['model'],
            dimension, hashlib.sha256((secret or '').encode()).hexdigest(),
            id(values.get('_model')), id(values.get('_client')),
            values.get('_batch_size'), values.get('_max_chars'))


def _collection_identity(collection):
    values = vars(collection)
    model = values.get('_model')
    metadata = vars(model) if model is not None else {}
    return (id(values.get('_client')), tuple(str(metadata.get(key, ''))
            for key in ('id', 'name', 'tenant', 'database')))


@dataclass(frozen=True)
class RuntimeIdentity:
    keys: tuple
    # Retain old references so process object ids cannot be recycled into a
    # seemingly identical runtime while this receipt remains live.
    references: tuple = field(compare=False, repr=False)


@dataclass(frozen=True)
class DocumentQueryReceipt:
    retrieval: object
    digest: str
    manager: object
    identity: RuntimeIdentity
    source: object


def identity(manager):
    vector = manager.vector_rag
    lanes = tuple(vector._lanes)
    if not vector.healthy or not lanes:
        raise ValueError('document runtime unavailable')
    keys = (id(manager), id(vector), tuple(
        (id(lane), id(lane.collection), id(lane.client), lane.name,
         lane.collection_name, lane.model, lane.dimension, lane.fingerprint,
         hashlib.sha256(str(lane.url).encode()).hexdigest(), lane.healthy,
         _client_identity(lane.client), _collection_identity(lane.collection))
        for lane in lanes))
    references = (manager, vector, *lanes, *(lane.collection for lane in lanes),
                  *(lane.client for lane in lanes),
                  *(vars(lane.client).get('_client') for lane in lanes),
                  *(vars(lane.collection).get('_client') for lane in lanes))
    return RuntimeIdentity(keys, references)


def capturing():
    return _ACTIVE.get() is not None


def validating():
    state = _ACTIVE.get()
    return bool(state and state['validation'])


@contextmanager
def capture_queries(*, validation=False):
    state = {'seen': False, 'receipts': [], 'completed': {}, 'validation': validation}
    token = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)


def completed(retrieval, bundle):
    # Called on the async task after to_thread returns, never from the worker.
    state = _ACTIVE.get()
    if state is not None and len(state['completed']) <= MAX_QUERIES:
        state['completed'][id(retrieval)] = bundle


def record_query(retrieval, results, planned):
    state = _ACTIVE.get()
    if state is None:
        return
    state['seen'] = True
    if not planned or len(state['receipts']) > MAX_QUERIES:
        return
    bundle = state['completed'].pop(id(retrieval), None)
    result = next((r for r in results if r.source_id == 'documents'), None)
    digest = projection_digest(result)
    receipt = None
    if bundle and digest:
        manager, before, after, strict_ok, source = bundle
        if strict_ok and before == after and source._manager is manager:
            receipt = DocumentQueryReceipt(copy.deepcopy(retrieval), digest, manager, before, source)
    state['receipts'].append(receipt)


def captured(state, packet=None):
    receipts = state['receipts']
    if not state['seen'] or len(receipts) > MAX_QUERIES or any(r is None for r in receipts):
        return None
    if not receipts and packet is not None and any(
            item.source_ref.startswith('doc:') for section in packet.sections
            if section.kind != 'recent_messages' for item in section.items):
        return None
    return tuple(receipts)


async def revalidate(receipts, request, scope_identity, timeout):
    if not isinstance(receipts, tuple) or len(receipts) > MAX_QUERIES:
        return False
    from .adapters.documents import DocumentSource
    from .candidates import RetrievalRequest, gather
    for receipt in receipts:
        if (not isinstance(receipt, DocumentQueryReceipt)
                or not isinstance(receipt.retrieval, RetrievalRequest)
                or scope_identity(receipt.retrieval.request) != scope_identity(request)
                or not isinstance(receipt.digest, str) or len(receipt.digest) != 64):
            return False
        if identity(receipt.manager) != receipt.identity:
            return False
        if receipt.source._manager is not receipt.manager:
            return False
        retrieval = copy.deepcopy(receipt.retrieval)
        with capture_queries(validation=True) as state:
            source = DocumentSource(manager=receipt.manager)
            source._expected_query_identity = receipt.identity
            source._expected_query_source = receipt.source
            results = await gather([source], retrieval, timeout_s=timeout)
            record_query(retrieval, results, True)
        current = captured(state)
        if (receipt.source._manager is not receipt.manager or not current
                or current[0].identity != receipt.identity or current[0].digest != receipt.digest):
            return False
    return True
