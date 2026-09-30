"""Private, call-local receipts for the personal-memory queries actually compiled."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .objective_reuse import projection_digest

_ACTIVE = ContextVar('personal_memory_reuse_capture', default=None)
MAX_QUERIES = 16


@dataclass(frozen=True)
class PersonalMemoryQueryReceipt:
    retrieval: object
    digest: str


def capturing():
    return _ACTIVE.get() is not None


def validating():
    state = _ACTIVE.get()
    return bool(state and state["validation"])


@contextmanager
def capture_queries(*, validation=False):
    state = {'seen': False, 'receipts': [], 'validation': validation}
    token = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)


def captured(state, packet=None):
    receipts = state['receipts']
    if not state['seen'] or len(receipts) > MAX_QUERIES or any(r is None for r in receipts):
        return None
    if not receipts and packet is not None:
        for section in packet.sections:
            if section.kind == "recent_messages":
                continue
            if any(item.source_ref.startswith("pmem:")
                   for item in section.items):
                return None  # Personal-memory bodies with no query backing are unknown.
    return tuple(receipts)


def record_query(retrieval, results, planned):
    state = _ACTIVE.get()
    if state is None:
        return
    state['seen'] = True
    if not planned:
        return
    if len(state['receipts']) > MAX_QUERIES:
        return  # Keep the overflow marker, never certify or retain a longer prefix.
    result = next((r for r in results if r.source_id == 'personal_memory'), None)
    digest = projection_digest(result)
    state['receipts'].append(PersonalMemoryQueryReceipt(copy.deepcopy(retrieval), digest) if digest else None)


async def revalidate(receipts, request, scope_identity, timeout):
    if not isinstance(receipts, tuple) or len(receipts) > MAX_QUERIES:
        return False
    from .adapters.memory import PersonalMemorySource
    from .candidates import RetrievalRequest, gather
    for receipt in receipts:
        if (not isinstance(receipt, PersonalMemoryQueryReceipt)
                or not isinstance(receipt.retrieval, RetrievalRequest)
                or scope_identity(receipt.retrieval.request) != scope_identity(request)):
            return False
        if (not isinstance(receipt.digest, str) or len(receipt.digest) != 64
                or any(c not in '0123456789abcdef' for c in receipt.digest)):
            return False
        # Re-run exactly the captured query through the same gate and normalization.
        with capture_queries(validation=True):
            results = await gather([PersonalMemorySource()], copy.deepcopy(receipt.retrieval), timeout_s=timeout)
        if projection_digest(results[0] if results else None) != receipt.digest:
            return False
    return True
