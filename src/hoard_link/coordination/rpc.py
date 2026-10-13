"""Portable typed repository calls; the Hub owns data, native engines own work."""
from __future__ import annotations

import dataclasses
from typing import Any

from . import council_contracts as contracts
from . import council_store as persistence

COUNCIL_METHODS = frozenset(name for name in vars(persistence.CouncilStore)
                           if not name.startswith('_') and name not in ('path', 'recover'))
GROUP_METHODS = frozenset(('claim_driver', 'release_driver', 'group', 'list', 'save', 'enqueue',
                          'turns', 'steps', 'start_step', 'update_step', 'update_turn', 'stop', 'recoverable', 'turn_status', 'archive'))
_TYPES = {cls.__name__: cls for cls in (contracts.CouncilSession, contracts.CouncilParticipant,
          contracts.CouncilMessage, contracts.CouncilTurn, contracts.CouncilTask, contracts.CouncilClaim,
          contracts.CouncilObjection, contracts.CouncilDecision)}

READ_METHODS = frozenset(name for name in COUNCIL_METHODS if name.startswith(('get_', 'list_')) or name in ('stats', 'idempotency_ref', 'last_close', 'driver', 'controls', 'control_sessions', 'pending_sessions')) | frozenset(('group', 'list', 'turns', 'steps', 'recoverable', 'turn_status'))


class UnknownOutcome(persistence.CouncilStoreError):
    """The storage acknowledgement was lost; reconcile IDs before any retry."""
    outcome_unknown = True

    def __init__(self, operation='', reference=None):
        self.operation, self.reference = operation, reference
        self.reconcile = 'Read the canonical record by its stable ID or expected revision before retrying'
        super().__init__('coordination', 'The operation may have completed; ' + self.reconcile)


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and type(value).__name__ in _TYPES:
        return {'record_type': type(value).__name__, 'value': value.to_dict()}
    if isinstance(value, tuple):
        return {'record_type': 'tuple', 'value': [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        # A native payload is data even if it happens to contain type-shaped keys.
        return {'record_type': 'mapping', 'value': {k: encode(v) for k, v in value.items()}}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict) or 'record_type' not in value:
        return value
    kind, body = value['record_type'], value['value']
    if kind == 'mapping':
        return {k: decode(v) for k, v in body.items()}
    if kind == 'tuple':
        return tuple(decode(v) for v in body)
    if kind not in _TYPES:
        raise ValueError('Unknown coordination record type')
    return _TYPES[kind].parse(body)


def error_payload(error: Exception) -> dict[str, Any]:
    fields = ('path', 'message', 'kind', 'row_id', 'session_id', 'expected', 'revision',
              'resource', 'holder_id', 'claim_id', 'key', 'turn_id', 'got', 'has_got')
    return {'type': type(error).__name__, 'detail': str(error),
            'fields': {key: getattr(error, key) for key in fields if hasattr(error, key)}}


def raise_error(payload: dict[str, Any]) -> None:
    kind, fields = payload.get('type'), payload.get('fields') or {}
    if payload.get('outcome_unknown'):
        raise UnknownOutcome(payload.get('operation', ''), payload.get('reference'))
    if kind == 'NotFound':
        raise persistence.NotFound(fields['kind'], fields['row_id'])
    if kind == 'RevisionConflict':
        raise persistence.RevisionConflict(fields['session_id'], fields.get('expected'), fields['revision'])
    if kind == 'ClaimConflict':
        raise persistence.ClaimConflict(fields['resource'], fields['holder_id'], fields['claim_id'])
    if kind == 'DuplicateTurn':
        raise persistence.DuplicateTurn(fields['key'], fields['turn_id'])
    if kind == 'KeyError':
        raise KeyError(payload.get('detail', 'Coordination record not found'))
    cls = persistence.CouncilStoreError if kind == 'CouncilStoreError' else contracts.CouncilError
    kwargs = {'got': fields.get('got')} if fields.get('has_got') else {}
    raise cls(fields.get('path', 'coordination'), fields.get('message') or payload.get('detail', 'Coordination call failed'), **kwargs)


class RepositoryClient:
    """Same repository operations over the native Hub API, without local shadow writes."""
    def __init__(self, transport, kind: str):
        self.transport = transport
        self.kind = kind

    def __getattr__(self, name: str):
        allowed = COUNCIL_METHODS if self.kind == 'council' else GROUP_METHODS
        if name not in allowed:
            raise AttributeError(name)

        def call(*args, **kwargs):
            result = self.transport('/api/coordination/repository', {'kind': self.kind, 'operation': name,
                                    'args': encode(args), 'kwargs': encode(kwargs)})
            if not isinstance(result, dict) or not result.get('ok'):
                error = (result or {}).get('error') if isinstance((result or {}).get('error'), dict) else {
                    'type': 'CouncilStoreError', 'detail': 'Shared coordination service unavailable'}
                if (name not in READ_METHODS and 'outcome_unknown' not in error and not (result or {}).get('status')
                        and error.get('type') == 'CouncilStoreError' and not error.get('fields')):
                    error = {**error, 'outcome_unknown': True, 'operation': name, 'reference': encode(args)}
                raise_error(error)
            return decode(result.get('result'))
        return call
