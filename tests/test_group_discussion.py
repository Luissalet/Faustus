"""Room members answer each other: @mentions hand the floor, [PASS] stays quiet."""
import asyncio

from src import group_discussion as d
from src.group_orchestrator import GroupCoordinator, GroupStore
from tests.test_group_orchestrator import Transcript, idle


def members():
    return [{'sessionId': 'glm', 'personaName': 'glm-5.3-flash-nvfp4', 'route': {'model': 'glm-5.3-flash-nvfp4'}},
            {'sessionId': 'qwen', 'personaName': 'qwen3.8-flash-next', 'route': {'model': 'qwen3.8-flash-next'}}]


def room(mode='round-robin', rounds=None):
    value = {'parentId': 'room', 'name': 'Room', 'mode': mode, 'executionMode': 'agent', 'workspace': '',
             'participants': members()}
    if rounds is not None:
        value['discussionRounds'] = rounds
    return value


class Scripted:
    """Answers from a script keyed by (round, member); records who spoke when."""
    def __init__(self, script):
        self.script, self.calls = script, []

    async def invoke(self, **kw):
        turn, member = kw['turn'], kw['member']
        key = (d.peer_round(turn), member['sessionId'])
        self.calls.append(key)
        await asyncio.sleep(0)
        return {'text': self.script.get(key, d.PASS_TEXT), 'detail': {}}

    async def stop(self, sid, rid):
        pass


def test_handles_include_short_unique_prefixes():
    found = d.handles(members())
    assert found['glm'] == ['glm-5.3-flash-nvfp4', 'glm']
    assert 'qwen' in found['qwen'] and 'qwen3' in found['qwen']


def test_mentions_by_handle_or_vocative_but_not_self():
    assert d.mentioned('@qwen can you check the parser?', members(), 'glm') == ['qwen']
    assert d.mentioned('I agree with @glm-5.3-flash-nvfp4.', members(), 'qwen') == ['glm']
    assert d.mentioned('Qwen, revisa esto.', members(), 'glm') == ['qwen']
    assert d.mentioned('@glm I will do it myself', members(), 'glm') == []
    assert d.mentioned('qwen3 tokenizer is fine', members(), 'glm') == []
    assert d.mentioned('email me@qwen.com', members(), 'glm') == []


def test_pass_detection():
    assert d.is_pass('[PASS]') and d.is_pass(' pass. ') and d.is_pass('[PASS] agree with Qwen') and d.is_pass('')
    assert not d.is_pass('I would pass the list by reference')


def test_parallel_round_gets_one_reaction_round_round_robin_needs_a_mention():
    turn = {'id': 't1', 'request_id': 'r1'}
    steps = [{'member_id': 'glm', 'status': 'done', 'text': 'Plan A'}, {'member_id': 'qwen', 'status': 'done', 'text': 'Plan B'}]
    plan = d.next_round(turn, steps, members(), 'parallel', 3)
    assert plan['round'] == 1 and plan['members'] == ['glm', 'qwen'] and plan['origin'] == 't1'
    assert d.next_round(turn, steps, members(), 'round-robin', 3) is None
    steps[1]['text'] = '@glm your plan misses the tests'
    assert d.next_round(turn, steps, members(), 'round-robin', 3)['members'] == ['glm']
    assert d.next_round(turn, steps, members(), 'round-robin', 0) is None
    last = {'id': 'p', 'request_id': d.peer_request_id('t1', 3)}
    assert d.next_round(last, steps, members(), 'round-robin', 3) is None


def test_discussion_is_uncapped_by_default_but_keeps_explicit_zero_and_positive_limits():
    assert d.rounds_limit({}) == -1
    assert d.rounds_limit({'discussionRounds': 50}) == 50
    steps = [{'member_id': 'glm', 'status': 'done', 'text': '@qwen check this'}]
    later = {'id': 'p', 'request_id': d.peer_request_id('t1', 20)}
    assert d.next_round(later, steps, members(), 'round-robin', -1)['round'] == 21
    assert d.next_round(later, steps, members(), 'round-robin', 3) is None
    assert d.next_round(later, steps, members(), 'round-robin', 0) is None
    assert '/-1' not in d.peer_prompt(21, -1)


async def test_mentioned_member_answers_in_a_follow_up_round(tmp_path):
    inv = Scripted({(0, 'glm'): 'Plan: parser first. @qwen can you write the tests?',
                    (0, 'qwen'): 'Agreed on the parser.',
                    (1, 'qwen'): 'Yes. @glm which fixtures should I use?',
                    (2, 'glm'): 'The Showdown dumps in tests/data.'})
    history = Transcript()
    c = GroupCoordinator(inv, history, store=GroupStore(tmp_path / 'groups.db'))
    c.adopt(room(), 'owner')
    await c.submit('room', 'owner', 'Add all Pokémon', 'k1')
    await idle(c)
    assert inv.calls == [(0, 'glm'), (0, 'qwen'), (1, 'qwen'), (2, 'glm'), ]
    turns = c.store.turns('room')
    assert [d.peer_round(t) for t in turns] == [0, 1, 2]
    assert all(t['status'] == 'done' for t in turns)
    assert all(t['request_id'].startswith(d.PEER_PREFIX) for t in turns[1:])
    assert d.origin(turns[2]) == turns[0]['id']
    # The last reply mentions nobody: the conversation ends there.
    assert not c.snapshot('room', 'owner')['busy']


async def test_parallel_answers_get_a_reaction_round_and_pass_ends_it(tmp_path):
    inv = Scripted({(0, 'glm'): 'Option A', (0, 'qwen'): 'Option B'})
    c = GroupCoordinator(inv, Transcript(), store=GroupStore(tmp_path / 'groups.db'))
    c.adopt(room('parallel'), 'owner')
    await c.submit('room', 'owner', 'Which?', 'k1')
    await idle(c)
    assert sorted(inv.calls[:2]) == [(0, 'glm'), (0, 'qwen')]
    # Reaction round taken in turns, both pass: nothing further.
    assert inv.calls[2:] == [(1, 'glm'), (1, 'qwen')]
    assert len(c.store.turns('room')) == 2


async def test_round_limit_and_zero_disables_follow_ups(tmp_path):
    loop = {(r, m): '@' + ('qwen' if m == 'glm' else 'glm') + ' again' for r in range(10) for m in ('glm', 'qwen')}
    inv = Scripted(loop)
    c = GroupCoordinator(inv, Transcript(), store=GroupStore(tmp_path / 'a.db'))
    c.adopt(room(rounds=2), 'owner')
    await c.submit('room', 'owner', 'go', 'k1')
    await idle(c)
    assert max(r for r, _ in inv.calls) == 2
    quiet = Scripted(loop)
    c2 = GroupCoordinator(quiet, Transcript(), store=GroupStore(tmp_path / 'b.db'))
    c2.adopt(room(rounds=0), 'owner')
    await c2.submit('room', 'owner', 'go', 'k1')
    await idle(c2)
    assert quiet.calls == [(0, 'glm'), (0, 'qwen')]


async def test_person_message_supersedes_a_queued_follow_up_round(tmp_path):
    store = GroupStore(tmp_path / 'groups.db')
    inv = Scripted({})
    c = GroupCoordinator(inv, Transcript(), store=store)
    c.adopt(room(), 'owner')
    peer, _ = store.enqueue('room', 'owner', d.peer_prompt(1, 3), d.peer_request_id('origin', 1), ['qwen'])
    person, _ = store.enqueue('room', 'owner', 'Actually, stop and do X', 'k2')
    c.kick('room', 'owner')
    await idle(c)
    statuses = {t['id']: t['status'] for t in store.turns('room')}
    assert statuses[peer] == 'stopped' and statuses[person] == 'done'
    assert (1, 'qwen') not in inv.calls


async def test_update_keeps_project_and_changes_rounds(tmp_path):
    c = GroupCoordinator(Scripted({}), Transcript(), store=GroupStore(tmp_path / 'groups.db'))
    c.adopt({**room(), 'projectId': 'silver'}, 'owner')
    updated = c.update('room', 'owner', {'discussionRounds': 5, 'name': 'Silver room'})
    assert updated['discussionRounds'] == 5 and updated['projectId'] == 'silver' and updated['name'] == 'Silver room'
    assert c.update('room', 'owner', {'discussionRounds': 99})['discussionRounds'] == 99


def test_room_transcript_keeps_the_members_rich_answer_and_skips_follow_up_prompts():
    from core.models import ChatMessage
    from fastapi import FastAPI
    from src.group_orchestrator import NativeGroupBridge
    from tests.test_group_native_bridge import Sessions, turn
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, FastAPI())
    t = turn()
    member = sessions.get_session('a')
    member.add_message(ChatMessage('system', 'boundary', metadata={'group_request_boundary': 'step'}))
    member.add_message(ChatMessage('assistant', 'Edited the parser', metadata={
        'tool_events': [{'tool': 'edit_file', 'command': 'parser.py', 'output': 'ok', 'exit_code': 0}],
        'thinking': 'Look at the parser first', 'response_time': 3.2, '_db_id': 'private', 'client_message_id': 'x'}))
    bridge.reply(t, {'id': 'step', 'member_id': 'a', 'text': 'Edited the parser', 'detail': {}}, 'owner')
    saved = sessions.get_session('parent').history[-1].metadata
    assert saved['tool_events'][0]['tool'] == 'edit_file' and saved['thinking'] == 'Look at the parser first'
    assert saved['response_time'] == 3.2 and saved['group_member_id'] == 'a' and saved['group_round'] == 0
    assert '_db_id' not in saved and 'client_message_id' not in saved and saved['group_pass'] is False
    peer = {**t, 'id': 'peer-turn', 'request_id': d.peer_request_id('turn', 1), 'message': d.peer_prompt(1, 3)}
    bridge.user(peer, 'owner')
    assert not any(m.role == 'user' for m in sessions.get_session('parent').history)


def test_member_context_is_a_group_chat_with_rules_and_the_persons_words():
    from core.models import ChatMessage
    from fastapi import FastAPI
    from src.group_orchestrator import NativeGroupBridge
    from tests.test_group_native_bridge import Sessions, turn
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, FastAPI())
    parent = sessions.get_session('parent')
    parent.add_message(ChatMessage('user', 'Earlier request', metadata={'group_turn_id': 'old'}))
    parent.add_message(ChatMessage('assistant', '[PASS]', metadata={'group_step_id': 's1', 'group_turn_id': 'old', 'group_model': 'b', 'group_pass': True}))
    parent.add_message(ChatMessage('assistant', '@a check the tests', metadata={'group_step_id': 's2', 'group_turn_id': 'old', 'group_model': 'b'}))
    t = {**turn(), 'id': 'peer', 'request_id': d.peer_request_id('old', 1), 'definition': {**turn()['definition'], 'mode': 'parallel'}}
    text = bridge._context(t, t['definition']['participants'][0])
    assert 'This is a group chat' in text and '@b' in text and d.PASS_TEXT in text
    assert '[Person]: Earlier request' in text and '[b] had nothing to add.' in text and '[b]: @a check the tests' in text
