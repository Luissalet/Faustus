"""Call-local receipts for strict learned-memory queries (standing and hybrid).

Standing receipts cover the no-query path (procedural rules/anti-patterns).
Hybrid receipts cover query-bearing retrieval through the read-only
``memory_engine.context_search`` primitive: they keep the database path, the
installed vector runtime identity (``None`` when the semantic lane is vetoed)
and the scoring clock of the capture.  Revalidation re-runs the same strict
query with that scoring clock and the CURRENT validity instant, so time decay
alone never invalidates a packet while data, membership, conflicts, expiry,
store path or embedding runtime changes do.  Never serialized.
"""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class HybridQueryReceipt:
    retrieval: object
    digest: str
    path: str
    identity: object
    clock: datetime
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


def hybrid(retrieval):
    return bool(str(retrieval.query or '').strip() and retrieval.allows('lexical'))


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
    if bundle and digest and len(bundle) == 3 and standing(retrieval):
        before, after, source = bundle
        if before == after:
            receipt = StandingQueryReceipt(copy.deepcopy(retrieval), digest, before, source)
    elif bundle and digest and len(bundle) == 6 and hybrid(retrieval):
        before, after, identity_before, identity_after, clock, source = bundle
        if (before == after and identity_before == identity_after
                and isinstance(clock, datetime) and clock.tzinfo is not None):
            receipt = HybridQueryReceipt(copy.deepcopy(retrieval), digest, before,
                                         identity_before, clock, source)
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
        kind = type(receipt)
        if (kind not in (StandingQueryReceipt, HybridQueryReceipt)
                or not isinstance(receipt.retrieval, RetrievalRequest)
                or not (standing if kind is StandingQueryReceipt else hybrid)(receipt.retrieval)
                or scope_identity(receipt.retrieval.request) != scope_identity(request)
                or not isinstance(receipt.digest, str) or len(receipt.digest) != 64
                or database_path() != receipt.path):
            return False
        if kind is HybridQueryReceipt and not _runtime_is_current(receipt):
            return False
        retrieval = copy.deepcopy(receipt.retrieval)
        with capture_queries(validation=True) as state:
            source = MemoryEngineSource()
            source._expected_database_path = receipt.path
            if kind is HybridQueryReceipt:
                source._expected_vector_identity = receipt.identity
                source._scoring_clock = receipt.clock
            results = await gather([source], retrieval, timeout_s=timeout)
            record_query(retrieval, results, True)
        current = captured(state)
        if (database_path() != receipt.path or not current or type(current[0]) is not kind
                or current[0].path != receipt.path or current[0].digest != receipt.digest):
            return False
        if kind is HybridQueryReceipt and (current[0].identity != receipt.identity
                                           or current[0].clock != receipt.clock):
            return False
    return True


def _runtime_is_current(receipt):
    """Same installed vector runtime (or still no semantic lane) — no getters."""
    if receipt.identity is None:
        return not receipt.retrieval.allows('semantic')
    from src import memory_engine
    from src.embedding_runtime_identity import vector_identity
    try:
        return vector_identity(memory_engine._installed_semantic_store()) == receipt.identity
    except Exception:
        return False
