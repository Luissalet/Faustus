"""Bind the native engines to the family's canonical room repositories at startup."""
from __future__ import annotations

from pathlib import Path

from src.constants import DATA_DIR
from src.hoard_link import family, _famsvc
from src.hoard_link.atomic import read_json, write_json_atomic
from src.hoard_link.coordination.rpc import RepositoryClient, READ_METHODS

_bound = False
_pending = False


def _transport(path, args):
    from src.family_services import configure
    configure()
    if _pending and not path.endswith('/bind'):
        return {'ok': False, 'error': {'type': 'CouncilStoreError',
                'outcome_unknown': False,
                'detail': 'Canonical coordination import is pending; original records are retained'}}
    status_code, result = family._post(path, args, 30)
    if isinstance(result, dict) and 'ok' in result:
        return {**result, 'status': status_code}
    writing = path.endswith('/bind') or args.get('operation') not in READ_METHODS
    return {'ok': False, 'error': {'type': 'CouncilStoreError',
        'outcome_unknown': writing and (status_code is None or status_code >= 500),
        'operation': args.get('operation') or 'bind', 'reference': args.get('args'),
        'detail': 'Shared coordination service unavailable'}}


def configure(group_coordinator=None):
    """Import before drivers start. A recorded binding never resumes local shadow writes."""
    global _bound, _pending
    from src.family_services import configure as configure_family
    from src.council import persistence
    configure_family()
    marker = Path(DATA_DIR) / 'coordination-binding.json'
    previous = read_json(marker, {}) or {}
    _pending = bool(previous.get('pending'))
    result = _famsvc.call_tool('hub', 'hub_coordination_config', {}, timeout_s=5)
    config = result.get('data') or {}
    if not previous.get('bound') or previous.get('pending'):
        if not result.get('ok') or config.get('authority') != 'hub':
            _pending = True
            write_json_atomic(marker, {'bound': True, 'pending': True, 'schema_version': 1})
        else:
            # Select one authority before the request: an uncertain import reply
            # must not restore the old stores and create divergent new records.
            _pending = True
            write_json_atomic(marker, {'bound': True, 'pending': True, 'schema_version': 1})
            imported = _transport('/api/coordination/bind', {'sources': {
                'council': str(Path(DATA_DIR) / 'council.db'), 'groups': str(Path(DATA_DIR) / 'groups.db')}})
            if imported.get('ok'):
                _pending = False
                write_json_atomic(marker, {'bound': True, 'pending': False, 'schema_version': 1,
                                           'imports': imported.get('imports', {})})
    with persistence._LOCK:
        persistence._STORE = RepositoryClient(_transport, 'council')
    if group_coordinator is not None:
        group_coordinator.store = RepositoryClient(_transport, 'groups')
    _bound = True
    return {'ok': not _pending, 'authority': 'hub', 'pending_import': _pending,
            'already_bound': bool(previous.get('bound'))}


def status():
    return {'ok': not _pending, 'authority': 'hub' if _bound else 'legacy-local',
            'bound': _bound, 'pending_import': _pending}


def resume_import(group_coordinator):
    if _pending:
        return configure(group_coordinator)
    return status()


def call(tool, args=None):
    from src.family_services import configure as configure_family
    configure_family()
    raw = family.call('hub', tool, args or {}, timeout=30)
    return raw.get('result') if isinstance(raw, dict) else {'ok': False, 'error': 'Shared coordination unavailable'}
