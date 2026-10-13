"""Old Faustus import paths use the same shared contracts and repositories."""
from src.contracts.base import ContractError
from src.council import contracts, persistence
from src.group_orchestrator import GroupStore
from src.hoard_link.coordination import base, council_contracts, council_store
from src.hoard_link.coordination.groups import GroupStore as SharedGroupStore


def test_step_acquisition_observes_membership_and_conditional_removal(tmp_path):
    store = GroupStore(tmp_path / 'groups.db')
    original = {'parentId': 'room', 'mode': 'round-robin', 'participants': [
        {'sessionId': name, 'personaName': name} for name in ('first', 'removed')]}
    store.save(original, 'alice')
    turn, _ = store.enqueue('room', 'alice', 'Work', 'request')
    first, removed = store.steps(turn)
    assert store.start_step(first['id'])['status'] == 'queued'
    store.update_step(first['id'], expected_status='queued', status='stopped')
    assert store.steps(turn)[0]['status'] == 'running'
    store.save({**original, 'participants': original['participants'][:1]}, 'alice')
    assert store.start_step(removed['id']) is None
    assert store.steps(turn)[1]['status'] == 'stopped'
    assert store.start_step(first['id'])['status'] == 'running'
    store.stop('room')
    assert store.start_step(first['id']) is None


def test_control_recovery_preserves_live_executor_and_marks_lost_lease_unknown(tmp_path):
    store = persistence.CouncilStore(path=str(tmp_path / 'council.db'))
    store.create_session(contracts.CouncilSession.parse({'id': 'room', 'owner': 'alice'}))
    assert store.claim_driver('room', 'old')
    ident = store.queue_control('room', 'steer', 'alice', 'alice', {'message': 'Work'})
    assert store.begin_control(ident)
    assert not store.claim_driver('room', 'new')
    assert store.controls('room')[0]['status'] == 'running'
    with store._db() as conn:
        conn.execute('UPDATE council_drivers SET lease_until=0 WHERE session_id="room"')
    assert 'room' in store.control_sessions()
    assert store.claim_driver('room', 'new')
    row = store.controls('room')[0]
    assert row['status'] == 'unknown'
    assert row['result']['outcome_unknown'] and row['result']['reference'] == ident
    assert not store.begin_control(ident)


def test_compatibility_imports_preserve_error_types_and_singleton_seams(tmp_path):
    assert ContractError is base.ContractError
    assert contracts is council_contracts
    assert persistence is council_store
    assert issubclass(GroupStore, SharedGroupStore)
    persistence.use_path(str(tmp_path / 'council.db'))
    try:
        assert persistence.store() is council_store.store()
    finally:
        persistence.use_path(None)


def test_native_council_service_lists_only_requested_space_without_running_models(tmp_path):
    from src.council.service import CouncilService
    store = persistence.CouncilStore(path=str(tmp_path / 'council.db'))
    for sid in ('ecosystem', 'project'):
        store.create_session(contracts.CouncilSession.parse({'id': sid, 'space_id': sid, 'owner': 'alice'}))
    service = CouncilService(store=store)
    assert {s.id for s in service.list(owner='alice')} == {'ecosystem', 'project'}
    assert [s.id for s in service.list(owner='alice', space_id='project')] == ['project']
    assert not service.list(owner='bob', space_id='project')


def test_native_group_store_uses_shared_space_filter(tmp_path):
    store = GroupStore(tmp_path / 'groups.db')
    for sid in ('ecosystem', 'project'):
        store.save({'parentId': sid, 'space_id': sid, 'participants': []}, 'alice')
    assert [g['parentId'] for g in store.list('alice', space_id='project')] == ['project']


def test_existing_mcp_create_and_list_tools_expose_space():
    from mcp_servers import council_server, groups_server
    assert 'space_id' in council_server.SPECS['council_create'][2]
    assert 'space_id' in council_server.SPECS['council_list'][2]
    assert 'space_id' in groups_server.SPECS['group_create'][2]
    assert 'space_id' in groups_server.SPECS['group_list'][2]


def test_uncertain_bind_does_not_restore_local_writes_and_reconciles_same_import(tmp_path, monkeypatch):
    import hashlib
    import json
    import types
    import pytest
    from src import family_services, shared_coordination as binding
    from src.hoard_link import family, _famsvc
    from src.hoard_link.coordination.rpc import RepositoryClient
    from src.group_orchestrator import GroupCoordinator

    path = tmp_path / 'groups.db'
    legacy = GroupStore(path)
    legacy.save({'parentId': 'original', 'participants': []}, 'alice')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    coordinator = GroupCoordinator(object(), object(), store=legacy)
    monkeypatch.setattr(binding, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(binding, '_bound', False)
    monkeypatch.setattr(binding, '_pending', False)
    monkeypatch.setattr(persistence, '_STORE', None)
    monkeypatch.setattr(family_services, 'configure', lambda: {})
    monkeypatch.setattr(_famsvc, 'call_tool', lambda *args, **kwargs: {'ok': True, 'data': {'authority': 'hub'}})
    calls = []
    def replied(path, args, timeout):
        calls.append((path, args))
        if len(calls) == 1:
            return None, None  # server may have imported; acknowledgement was lost
        return 200, {'ok': True, 'imports': {'groups': {'already_imported': True}}}
    monkeypatch.setattr(family, '_post', replied)
    result = binding.configure(coordinator)
    assert not result['ok'] and result['pending_import']
    assert isinstance(coordinator.store, RepositoryClient)
    assert isinstance(persistence.store(), RepositoryClient)
    with pytest.raises(persistence.CouncilStoreError):
        coordinator.store.save({'parentId': 'divergent', 'participants': []}, 'alice')
    assert len(calls) == 1  # the pending import prevented a shadow or uncertain write
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    reconciled = binding.configure(coordinator)
    assert reconciled['ok'] and not reconciled['pending_import']
    assert calls[0] == calls[1]
    assert json.loads((tmp_path / 'coordination-binding.json').read_text())['bound']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_default_group_store_is_deferred_until_authority_is_selected(tmp_path, monkeypatch):
    from src import group_orchestrator as group
    monkeypatch.setattr(group, 'DATA_DIR', str(tmp_path))
    coordinator = group.GroupCoordinator(object(), object())
    assert not (tmp_path / 'groups.db').exists()
    assert coordinator.store.list(None) == []
    assert (tmp_path / 'groups.db').exists()
