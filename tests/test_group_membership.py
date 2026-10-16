from types import SimpleNamespace
import pytest

from src.group_orchestrator import GroupCoordinator, GroupStore, NativeGroupBridge
from src.hoard_link.coordination.council_store import RevisionConflict


class Sessions:
    def __init__(self):
        self.rows = {}

    def create_session(self, ident, name, endpoint, model, **kwargs):
        session = SimpleNamespace(id=ident, name=name, owner=kwargs.get('owner'), history=[], headers={},
                                  add_message=lambda message: None)
        self.rows[ident] = session
        return session

    def get_session(self, ident):
        return self.rows[ident]

    def save_sessions(self):
        pass


def test_edit_members_preserves_room_sessions_history_and_stops_removed_queued_steps(tmp_path):
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, None)
    coordinator = GroupCoordinator(None, bridge, store=GroupStore(tmp_path/'groups.db'))
    models = [{'route': {'model': name, 'endpointUrl': 'http://127.0.0.1:65534'}, 'personaName': name}
              for name in ('Planner', 'Reader', 'Reviewer')]
    original = coordinator.adopt(bridge.create(models, 'round-robin', 'alice', name='Original'), 'alice')
    turn, _ = coordinator.store.enqueue(original['parentId'], 'alice', 'Work', 'request')
    original_steps = coordinator.store.steps(turn)
    coordinator.store.update_step(original_steps[0]['id'], status='done', text='Completed read')
    removed = original['participants'][2]['sessionId']
    observed = coordinator.store.group(original['parentId'], 'alice')['revision']
    edited = coordinator.update(original['parentId'], 'alice', {'name': 'Renamed',
        'participants': original['participants'][:2]}, observed)
    assert edited['parentId'] == original['parentId']
    assert [p['sessionId'] for p in edited['participants']] == [p['sessionId'] for p in original['participants'][:2]]
    assert len(sessions.rows) == 4  # Parent and the same three original private sessions.
    assert sessions.get_session(original['parentId']).name == '[GRP] Renamed'
    assert coordinator.store.steps(turn)[0]['text'] == 'Completed read'
    assert next(s for s in coordinator.store.steps(turn) if s['member_id'] == removed)['status'] == 'stopped'
    current = coordinator.snapshot(original['parentId'], 'alice')
    assert current['turns'][0]['participants'][2]['sessionId'] == removed
    with pytest.raises(RevisionConflict):
        coordinator.update(original['parentId'], 'alice', {'name': 'Stale'}, observed)
    assert coordinator.store.group(original['parentId'], 'alice')['name'] == 'Renamed'


def test_directed_turn_addresses_only_selected_member_after_edit(tmp_path):
    store = GroupStore(tmp_path/'groups.db')
    store.save({'parentId': 'room', 'participants': [{'sessionId': 'one', 'personaName': 'One'},
               {'sessionId': 'two', 'personaName': 'Two'}]}, 'alice')
    turn, _ = store.enqueue('room', 'alice', 'Only one', 'request', ['two'])
    assert [s['member_id'] for s in store.steps(turn)] == ['two']


def test_member_policy_is_persistent_and_does_not_change_old_queued_turns(tmp_path):
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, None)
    coordinator = GroupCoordinator(None, bridge, store=GroupStore(tmp_path/'groups.db'))
    members = [{'route': {'model': name, 'endpointUrl': 'http://127.0.0.1:65534'},
                **({'autonomyPreset': 'read_only', 'inputTokenBudget': 180000} if name == 'Reviewer' else {})}
               for name in ('Implementer', 'Reviewer')]
    original = coordinator.adopt(bridge.create(members, 'round-robin', 'alice'), 'alice')
    turn_id, _ = coordinator.store.enqueue(original['parentId'], 'alice', 'Review', 'old')
    observed = coordinator.store.group(original['parentId'], 'alice')
    observed['participants'][1]['inputTokenBudget'] = 90000
    updated = coordinator.update(original['parentId'], 'alice', {'participants': observed['participants']}, observed['revision'])
    assert updated['participants'][1]['autonomyPreset'] == 'read_only'
    assert updated['participants'][1]['inputTokenBudget'] == 90000
    assert updated['participants'][1]['sessionId'] == original['participants'][1]['sessionId']
    assert coordinator.store.turns(original['parentId'])[0]['definition']['participants'][1]['autonomyPreset'] == 'read_only'
    assert coordinator.store.turns(original['parentId'])[0]['definition']['participants'][1]['inputTokenBudget'] == 180000


def test_bad_policy_is_rejected_before_creating_any_session():
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, None)
    members = [{'route': {'model': 'reviewer', 'endpointUrl': 'http://127.0.0.1:65534'}, 'autonomyPreset': 'typo'}] * 2
    with pytest.raises(ValueError):
        bridge.create(members, 'round-robin', 'alice')
    assert not sessions.rows


def test_member_context_cursor_reads_new_peer_results_and_keeps_parallel_replies_unread():
    from core.models import ChatMessage
    sessions = Sessions()
    parent = sessions.create_session('room', 'Room', '', '', owner='alice')
    own = sessions.create_session('member', 'Member', '', '', owner='alice')
    parent.history = [ChatMessage('assistant', 'Old result', metadata={'group_step_id': 'old', 'group_model': 'Peer'}),
                      ChatMessage('assistant', 'New result', metadata={'group_step_id': 'new', 'group_model': 'Peer', 'group_turn_id': 'current'})]
    own.history = [ChatMessage('system', 'Old context', metadata={'group_seen_cursor': 'group_step_id:old'})]
    bridge = NativeGroupBridge(sessions, None, None)
    member = {'sessionId': 'member', 'personaName': 'Member'}
    turn = {'group_id': 'room', 'id': 'current', 'definition': {'mode': 'parallel'}}
    parallel = bridge._context_packet(turn, member)
    assert 'Old result' not in parallel['text'] and 'New result' not in parallel['text']
    assert parallel['cursor'] == 'group_step_id:old'
    turn['id'] = 'next'
    serial = bridge._context_packet(turn, member)
    assert 'Old result' not in serial['text'] and 'New result' in serial['text']
    assert serial['cursor'] == 'group_step_id:new'
