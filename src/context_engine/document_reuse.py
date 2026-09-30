"""Private receipts for actual strict document queries; never serialized."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from src.embedding_runtime_identity import RuntimeIdentity, vector_identity

from .objective_reuse import projection_digest

_ACTIVE = ContextVar('document_query_capture', default=None)
MAX_QUERIES = 16


@dataclass(frozen=True)
class DocumentQueryReceipt:
    retrieval: object
    digest: str
    manager: object
    identity: RuntimeIdentity
    source: object


def identity(manager):
    vector = vector_identity(manager.vector_rag)
    return RuntimeIdentity((id(manager), *vector.keys), (manager, *vector.references))


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
