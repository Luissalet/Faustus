"""Call-local receipts for strict standing learned-memory queries only."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from .objective_reuse import projection_digest

_ACTIVE = ContextVar('standing_memory_capture', default=None)
MAX_QUERIES = 16


@dataclass(frozen=True)
class StandingQueryReceipt:
    retrieval: object
    digest: str
    path: str
    source: object


def database_path():
    from src import memory_engine
    return str(Path(memory_engine.db_path()).absolute())


def capturing(): return _ACTIVE.get() is not None


def validating():
    state = _ACTIVE.get()
    return bool(state and state['validation'])


def standing(retrieval):
    return (not (str(retrieval.query or '').strip() and retrieval.allows('lexical'))
            and retrieval.allows('mandatory'))


@contextmanager
def capture_queries(*, validation=False):
    state = {'seen': False, 'receipts': [], 'completed': {}, 'validation': validation}
    token = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)


def completed(retrieval, bundle):
    state = _ACTIVE.get()
    if state is not None and len(state['completed']) <= MAX_QUERIES:
        state['completed'][id(retrieval)] = bundle


def record_query(retrieval, results, planned):
    state = _ACTIVE.get()
    if state is None: return
    state['seen'] = True
    if not planned or len(state['receipts']) > MAX_QUERIES: return
    bundle = state['completed'].pop(id(retrieval), None)
    result = next((row for row in results if row.source_id == 'memory_engine'), None)
    digest = projection_digest(result)
    receipt = None
    if bundle and digest:
        before, after, source = bundle
        if before == after:
            receipt = StandingQueryReceipt(copy.deepcopy(retrieval), digest, before, source)
    state['receipts'].append(receipt)


def captured(state, packet=None):
    receipts = state['receipts']
    if not state['seen'] or len(receipts) > MAX_QUERIES or any(r is None for r in receipts): return None
    if not receipts and packet is not None and any(item.source_ref.startswith('mem:')
            for section in packet.sections if section.kind != 'recent_messages' for item in section.items):
        return None
    return tuple(receipts)


async def revalidate(receipts, request, scope_identity, timeout):
    if not isinstance(receipts, tuple) or len(receipts) > MAX_QUERIES: return False
    from .adapters.memory import MemoryEngineSource
    from .candidates import RetrievalRequest, gather
    for receipt in receipts:
        if (not isinstance(receipt, StandingQueryReceipt)
                or not isinstance(receipt.retrieval, RetrievalRequest)
                or not standing(receipt.retrieval)
                or scope_identity(receipt.retrieval.request) != scope_identity(request)
                or not isinstance(receipt.digest, str) or len(receipt.digest) != 64
                or database_path() != receipt.path):
            return False
        retrieval = copy.deepcopy(receipt.retrieval)
        with capture_queries(validation=True) as state:
            source = MemoryEngineSource()
            source._expected_database_path = receipt.path
            results = await gather([source], retrieval, timeout_s=timeout)
            record_query(retrieval, results, True)
        current = captured(state)
        if (database_path() != receipt.path or not current or current[0].path != receipt.path
                or current[0].digest != receipt.digest): return False
    return True
