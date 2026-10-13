from src import shared_coordination
from src.agent_tools.subagent_tools import FileLockRegistry
from src.council.ledger import FileLockRegistryBackend, default_claims_backend


def test_two_native_rooms_and_delegations_use_one_shared_reservation_service(tmp_path, monkeypatch):
    from src.hoard_link.coordination.spaces import DEFAULT_SPACE_ID
    held = {}
    monkeypatch.setattr(shared_coordination, '_bound', True)
    def transport(tool, args):
        assert tool == 'hub_coordination_resources'
        owner, action = args['owner'], args['action']
        resources = args.get('resources', [])
        clashes = [{'owner': held[r], 'resource': r} for r in resources if r in held and held[r] != owner]
        if action == 'check': return {'ok': True, 'conflicts': clashes}
        if action == 'claim':
            if clashes: return {'ok': False, 'conflicts': clashes}
            held.update({r: owner for r in resources})
        if action == 'release':
            released = [r for r in resources if held.get(r) == owner]
            for r in released: held.pop(r)
            return {'ok': True, 'released': released}
        return {'ok': True}
    monkeypatch.setattr(shared_coordination, 'call', transport)
    first, second = FileLockRegistry(str(tmp_path)), FileLockRegistry(str(tmp_path))
    assert first.claim('writer', ['a.txt', 'b.txt']) == []
    assert second.blocked_by('writer', ['a.txt'])
    assert second.claim('writer', ['a.txt']) == ['a.txt']
    backend = FileLockRegistryBackend(first)
    assert backend.release('a.txt', 'writer')
    assert not second.claim('writer', ['a.txt'])
    assert second.blocked_by('writer', ['b.txt'])
    first.release('writer'); second.release('writer')
    assert not held
    assert first.claim('writer', ['failed.txt'], provisional=True) == []
    first.settle('writer', ['failed.txt'], False)
    assert not held


def test_bound_registry_does_not_silently_make_local_claims_after_transport_failure(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(shared_coordination, '_bound', True)
    monkeypatch.setattr(shared_coordination, 'call', lambda *args: {'ok': False, 'error': 'Disconnected'})
    registry = FileLockRegistry(str(tmp_path))
    with pytest.raises(RuntimeError, match='Shared reservations'):
        registry.claim('writer', ['a.txt'])
    assert not registry.owner
