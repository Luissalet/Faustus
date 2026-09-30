"""Private, call-local receipts for the objectives queries actually compiled."""
import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_ACTIVE = ContextVar('objective_reuse_capture', default=None)
MAX_QUERIES = 16


@dataclass(frozen=True)
class ObjectiveQueryReceipt:
    retrieval: object
    digest: str


def capturing():
    return _ACTIVE.get() is not None


@contextmanager
def capture_queries():
    state = {'seen': False, 'receipts': []}
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
            if any(item.source_type == "objective" or item.source_ref.startswith("objective:")
                   for item in section.items):
                return None  # Objective bodies with no query backing are unknown.
    return tuple(receipts)


def projection_digest(result):
    if result is None or not result.ok():
        return None
    rows = []
    for candidate in result.candidates:
        row = candidate.to_dict()
        row.pop('candidate_id', None)  # Random attempt identity, not source content.
        rows.append(row)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def record_query(retrieval, results, planned):
    state = _ACTIVE.get()
    if state is None:
        return
    state['seen'] = True
    if not planned:
        return
    if len(state['receipts']) > MAX_QUERIES:
        return  # Keep the overflow marker, never certify or retain a longer prefix.
    result = next((r for r in results if r.source_id == 'objectives'), None)
    digest = projection_digest(result)
    state['receipts'].append(ObjectiveQueryReceipt(copy.deepcopy(retrieval), digest) if digest else None)


async def revalidate(receipts, request, scope_identity, timeout):
    if not isinstance(receipts, tuple) or len(receipts) > MAX_QUERIES:
        return False
    from .adapters.objectives import ObjectivesSource
    from .candidates import RetrievalRequest, gather
    for receipt in receipts:
        if (not isinstance(receipt, ObjectiveQueryReceipt)
                or not isinstance(receipt.retrieval, RetrievalRequest)
                or scope_identity(receipt.retrieval.request) != scope_identity(request)):
            return False
        if (not isinstance(receipt.digest, str) or len(receipt.digest) != 64
                or any(c not in '0123456789abcdef' for c in receipt.digest)):
            return False
        # Re-run exactly the captured query through the same gate and normalization.
        with capture_queries():
            results = await gather([ObjectivesSource()], copy.deepcopy(receipt.retrieval), timeout_s=timeout)
        if projection_digest(results[0] if results else None) != receipt.digest:
            return False
    return True
