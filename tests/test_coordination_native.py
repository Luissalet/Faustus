import asyncio
import json
import hashlib
from dataclasses import replace

import pytest

from src.coordination import runtime
from src.coordination.bindings import Binding, CoordinationError, OPERATIONS, StaleBinding, UnknownOutcome
from src.coordination.client import Client, compact_board
from src.coordination.store import Store
from src.coordination.tools import NAMES


class Manager:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.after_call = None
        self.disabled = set()
        self.recent_schema = None

    def get_all_tools(self):
        suffixes = {"inbox", "board", "task", "locks", "heartbeat", "task_claim", "checkpoint", "task_review", "post"}
        result = [{"qualified_name": f"mcp__hub-a__hub_agora_{suffix}", "is_disabled": suffix in self.disabled}
                  for suffix in suffixes]
        next(tool for tool in result if tool["qualified_name"].endswith("_heartbeat"))["input_schema"] = {
            "type": "object", "properties": {"renew_resources": {"type": "array", "items": {"type": "string"}}}}
        if self.recent_schema is not None:
            result.append({"qualified_name": "mcp__hub-a__hub_agora_thread_recent",
                           "input_schema": self.recent_schema, "is_disabled": "thread_recent" in self.disabled})
        return result

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        suffix = name.split("hub_agora_")[1]
        result = self.responses.get(suffix, {"ok": True})
        if callable(result):
            result = result(args)
        await asyncio.sleep(0)
        if self.after_call:
            self.after_call()
        return result if "exit_code" in result else {"exit_code": 0, "structured_content": result}


@pytest.fixture
def native(tmp_path):
    store = Store(tmp_path)
    binding = store.save(Binding(id="b", owner="owner-a", project_id="p", session_id="s", connector_id="hub-a",
                                actor="faustus-native", operations=tuple(OPERATIONS), repos=("Writer",),
                                resources=("path:Writer/src",), thread_ids=(32,)))
    manager = Manager()
    client = Client(store, binding, manager, revalidate=lambda _: None)
    manager.responses["board"] = {"ok": True, "tasks": [{"id": 7, "repo": "Writer", "owner": "claude"}],
                                  "locks": [], "agents": [], "counts": {"review": 1}}
    manager.responses['task'] = {'ok': True, 'task': task(), 'messages': []}
    manager.responses['heartbeat'] = lambda args: {'ok':True,'lock_renewal':{'mode':'selected','renewed_resources':args['renew_resources']}}
    return store, binding, manager, client


def task():
    return {"id": 7, "repo": "Writer", "paths": ["src/file.ts"], "owner": "faustus-native",
            "status": "in_progress", "submission_revision": 2, "latest_checkpoint": None}


def test_board_current_cross_repo_coordination_context_is_read_only_and_project_first(native):
    _store, binding, _manager, _client = native
    board = {"tasks": [
        {"id": 149, "repo": "Faustus", "status": "review", "submission_revision": 3, "owner": "claude"},
        {"id": 133, "repo": "Writer", "status": "done", "submission_revision": 2, "owner": "claude"},
        {"id": 136, "repo": "Writer", "status": "claimed", "owner": binding.actor},
    ]}
    result = compact_board(board, binding)
    assert [row['id'] for row in result['tasks']] == [133, 136, 149]
    cross_repo = result['tasks'][-1]
    assert cross_repo['read_only'] is True
    assert cross_repo['scope'] == 'global_context'
    assert cross_repo['status'] == 'review' and cross_repo['submission_revision'] == 3
    assert result['tasks'][0]['status'] == 'done'


@pytest.mark.asyncio
async def test_heartbeat_records_metadata_and_derives_only_scoped_owned_renewals(native):
    _, binding, manager, client = native
    manager.responses['locks'] = {'ok': True, 'capabilities':{'heartbeat_selective_renewal':True}, 'locks': [
        {'owner':binding.actor,'resource':'path:writer/src/file.ts'},
        {'owner':binding.actor,'resource':'model:reserved-qa'},
        {'owner':binding.actor,'resource':'path:other/private-token.env'},
        {'owner':'other','resource':'path:writer/src/foreign.ts'}]}
    result = await client.execute('heartbeat', {'doing':'scoped project work'})
    assert manager.calls[-1][1] == {'doing':'scoped project work','agent':binding.actor,
                                   'renew_resources':['path:writer/src/file.ts'], 'space_id': binding.space_id}
    assert result['ok'] and result['advisories'][0]['count'] == 2
    assert 'private-token' not in json.dumps(result)


@pytest.mark.asyncio
async def test_heartbeat_no_scoped_locks_reports_state_with_empty_renewal(native):
    _, binding, manager, client = native
    manager.responses['locks'] = {'ok':True,'capabilities':{'heartbeat_selective_renewal':True},'locks':[{'owner':binding.actor,'resource':'model:reserved-qa'}]}
    assert (await client.execute('heartbeat', {'state':'waiting'}))['ok']
    assert manager.calls[-1][1]['renew_resources'] == []


@pytest.mark.asyncio
async def test_model_cannot_choose_heartbeat_renewal_or_actor(native):
    _, _, manager, client = native
    for args in ({'renew_resources':['model:reserved-qa']},{'agent':'luis'}):
        with pytest.raises(CoordinationError):
            await client.execute('heartbeat',args)
    assert manager.calls == []


@pytest.mark.asyncio
async def test_new_local_catalog_cannot_renew_via_an_old_live_hub(native):
    _, binding, manager, client = native
    manager.responses['locks']={'ok':True,'locks':[{'owner':binding.actor,'resource':'model:reserved-qa'}]}
    result=await client.execute('heartbeat',{})
    assert result['status']=='partial' and result['heartbeat_recorded'] is False
    assert result['result_status']=='partial'
    assert len(manager.calls)==1 and manager.calls[0][0].endswith('_locks')


@pytest.mark.asyncio
@pytest.mark.parametrize('locks',[[],[{'owner':'faustus-native','resource':'path:writer/src/file.ts'}]])
async def test_old_live_hub_legacy_heartbeat_with_no_outside_locks_and_no_receipt(native,locks):
    _,binding,manager,client=native
    manager.responses['locks']={'ok':True,'locks':locks}
    manager.responses['heartbeat']={'ok':True,'agent':binding.actor}
    result=await client.execute('heartbeat',{'doing':'legacy metadata'})
    assert result['heartbeat_recorded'] is True and result['renewal_mode']=='legacy_all'
    assert manager.calls[-1][1]=={'doing':'legacy metadata','agent':binding.actor,'space_id':binding.space_id}
    assert result['advisories'][0]['code']=='legacy_renewal_snapshot'


@pytest.mark.asyncio
async def test_new_hub_with_old_catalog_and_all_inside_uses_legacy_mode_all_receipt(native):
    _,binding,manager,client=native
    original=manager.get_all_tools
    def old_catalog():
        tools=original()
        next(t for t in tools if t['qualified_name'].endswith('_heartbeat')).pop('input_schema')
        return tools
    manager.get_all_tools=old_catalog
    manager.responses['locks']={'ok':True,'capabilities':{'heartbeat_selective_renewal':True},
        'locks':[{'owner':binding.actor,'resource':'path:writer/src/file.ts'}]}
    manager.responses['heartbeat']={'ok':True,'lock_renewal':{'mode':'all','renewed_resources':['path:writer/src/file.ts']}}
    result=await client.execute('heartbeat',{})
    assert result['heartbeat_recorded'] and result['renewal_mode']=='legacy_all'
    assert 'renew_resources' not in manager.calls[-1][1]


@pytest.mark.asyncio
async def test_legacy_receipt_showing_a_new_outside_lock_is_unknown_not_selective_success(native):
    _,binding,manager,client=native
    manager.responses['locks']={'ok':True,'locks':[]}
    manager.responses['heartbeat']={'ok':True,'lock_renewal':{'mode':'all','renewed_resources':['model:new-after-snapshot']}}
    with pytest.raises(UnknownOutcome):
        await client.execute('heartbeat',{})


@pytest.mark.asyncio
async def test_scope_revoked_after_lock_read_cannot_send_heartbeat(native):
    store,binding,manager,client=native
    manager.responses['locks']={'ok':True,'capabilities':{'heartbeat_selective_renewal':True},'locks':[]}
    manager.after_call=lambda:store.save(replace(binding,active=False),expected_revision=1)
    with pytest.raises(StaleBinding):
        await client.execute('heartbeat',{})
    assert len(manager.calls)==1


@pytest.mark.asyncio
async def test_unmatched_renewal_receipt_is_unknown_not_a_fake_success(native):
    _,binding,manager,client=native
    manager.responses['locks']={'ok':True,'capabilities':{'heartbeat_selective_renewal':True},'locks':[]}
    manager.responses['heartbeat']={'ok':True,'lock_renewal':{'mode':'all','renewed_resources':['model:reserved-qa']}}
    with pytest.raises(UnknownOutcome):
        await client.execute('heartbeat',{})


def test_board_context_stays_bounded_with_project_priority_and_explicit_omission(native):
    _store, binding, _manager, _client = native
    project = [{"id": n, "repo": "Writer"} for n in range(1, 81)]
    context = [{"id": n, "repo": "Faustus"} for n in range(81, 141)]
    result = compact_board({"tasks": context + project}, binding)
    assert len(result['tasks']) == 8
    assert result['tasks_omitted'] == 132
    rows = []
    offset = 0
    while offset is not None:
        page = compact_board({'tasks': context + project}, binding, offset=offset)
        assert page['digest'] == result['digest']
        rows.extend(page['tasks'])
        offset = page['next_offset']
    assert [row['id'] for row in rows[:80]] == list(range(1, 81))
    assert all(row['read_only'] for row in rows[80:])


@pytest.mark.asyncio
async def test_post_to_delivered_task_thread_resolves_task_without_model_task_id(native):
    store, binding, manager, client = native
    observed = {**task(), 'thread_id': 90}
    manager.responses['task']['task'] = observed
    receipt = store.observe_task(binding, observed)
    with pytest.raises(CoordinationError):
        await client.execute('post', {'thread_id': 90, 'body': 'A synthetic task reply'})
    assert manager.calls == []
    store.confirm_task_delivery(binding, receipt)
    await client.execute('post', {'thread_id': 90, 'body': 'A synthetic task reply'})
    assert manager.calls[-1][1] == {'thread_id': 90, 'body': 'A synthetic task reply', 'agent': binding.actor, 'space_id':binding.space_id}


def recent_contract(manager, messages):
    manager.recent_schema = {'type': 'object', 'additionalProperties': False, 'required': ['thread_id'],
                            'properties': {'thread_id': {'type': 'integer', 'minimum': 1},
                                           'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                                           'before_id': {'type': 'integer', 'minimum': 1}}}
    manager.responses['thread_recent'] = {'ok': True, 'thread': {'id': 32},
        'messages': [{**m, 'body_chars': len(m['body']), 'body_digest': hashlib.sha256(m['body'].encode()).hexdigest(),
                      'body_truncated': False} for m in messages],
        'pagination': {'has_more': True, 'next_before_id': messages[0]['id'] if messages else None},
        'hub_acknowledged': False}


@pytest.mark.asyncio
async def test_recent_configured_thread_exposes_latest_without_skipping_unfetched_history(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': [
        {'id': i, 'thread_id': 32, 'author': 'claude', 'body': 'Old state'} for i in range(1, 101)]}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'author': 'claude', 'body': 'Current reparto: 133 is done'}])
    result = await client.execute('poll', {})
    assert result['cursor'] == 100 and result['has_more']
    assert result['recent'][0]['id'] == 1500
    assert result['recent'][0]['body_excerpt'] == 'Current reparto: 133 is done'
    assert result['recent'][0]['detail_available']
    assert manager.calls[-1][1] == {'thread_id': 32, 'limit': 5, 'space_id':binding.space_id}
    assert store.cursor(binding) == 100
    assert store.detail(binding, 1500, expected_digest=result['recent'][0]['digest'])['body'] == 'Current reparto: 133 is done'
    assert all('agent' not in args for name, args in manager.calls if name.endswith('_thread_recent'))
    assert result['hub_acknowledged'] is False


@pytest.mark.asyncio
async def test_recent_unavailable_is_explicit_and_never_calls_legacy_full_thread(native):
    _store, _binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    result = await client.execute('poll', {})
    assert result['recent'] == []
    assert result['recent_unavailable']
    assert [name.rsplit('hub_agora_', 1)[-1] for name, _ in manager.calls] == ['inbox', 'board']


@pytest.mark.asyncio
async def test_truncated_recent_body_keeps_original_digest_and_is_never_cached_as_complete(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'body': 'Original complete body'}])
    message = manager.responses['thread_recent']['messages'][0]
    digest = message['body_digest']
    message.update(body='Original', body_truncated=True)
    result = await client.execute('poll', {})
    assert result['recent'][0]['digest'] == digest
    assert result['recent'][0]['detail_available'] is False
    assert result['recent_detail_unavailable'][0]['message_id'] == 1500
    assert store.cursor(binding) == 0
    with pytest.raises(CoordinationError):
        store.detail(binding, 1500, expected_digest=digest)


@pytest.mark.asyncio
async def test_recent_replay_preserves_delivered_receipts_and_filtered_cursor(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': [{'id': 100, 'thread_id': 32, 'body': 'Unrelated history'}]}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'body': 'Latest complete body'}])
    first = await client.execute('poll', {'thread_ids': [32]})
    ref = first['recent'][0]
    assert ref['thread_ids'] == [32]
    result = await client.execute('detail', {'message_id': 1500, 'digest': ref['digest'], 'thread_ids': ref['thread_ids']})
    store.confirm_delivery(binding, result['receipt'], threads=[32])
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    await client.execute('poll', {'thread_ids': [32]})
    assert store.pending(binding, [32]) == []
    assert store.cursor(binding, [32]) == 100 and store.cursor(binding) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [{}, {'properties': []}, {'properties': {'limit': 'invalid'}},
                                   {'properties': {'limit': {'maximum': 100}}}])
async def test_recent_incompatible_schema_cannot_trigger_new_or_legacy_reads(native, schema):
    _store, _binding, manager, client = native
    manager.recent_schema = schema
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    result = await client.execute('poll', {})
    assert result['recent_unavailable'] and not result['recent']
    assert len(manager.calls) == 2


@pytest.mark.asyncio
async def test_recent_disabled_reader_and_read_failure_are_explicit(native):
    _store, _binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    recent_contract(manager, [])
    client.disabled_tools.add('hub_agora_thread_recent')
    disabled = await client.execute('poll', {})
    assert disabled['recent_unavailable'] and len(manager.calls) == 2
    client.disabled_tools.clear()
    manager.responses['thread_recent'] = {'exit_code': 1, 'error': 'Read unavailable'}
    failed = await client.execute('poll', {})
    assert failed['recent_unavailable']
    assert failed['cursor'] == 0 and failed['hub_acknowledged'] is False


@pytest.mark.asyncio
async def test_revocation_during_recent_read_never_commits_inbox_or_fresh_details(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': [{'id': 100, 'thread_id': 32, 'body': 'Old page'}]}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'body': 'Fresh page'}])
    def revoke_on_recent():
        if manager.calls[-1][0].endswith('_thread_recent'):
            store.save(replace(binding, active=False), expected_revision=1)
    manager.after_call = revoke_on_recent
    with pytest.raises(StaleBinding):
        await client.execute('poll', {})
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM details').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM cursors').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_runtime_connector_revocation_during_recent_cannot_be_swallowed_by_fallback(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': [{'id': 100, 'thread_id': 32, 'body': 'Old page'}]}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'body': 'Fresh page'}])
    revoked = False
    def invalidate_connector():
        nonlocal revoked
        if manager.calls[-1][0].endswith('_thread_recent'):
            revoked = True
    def validate(_binding):
        if revoked:
            raise CoordinationError('bound connector is disabled by runtime policy')
    manager.after_call = invalidate_connector
    client.revalidate = validate
    with pytest.raises(CoordinationError, match='disabled by runtime policy'):
        await client.execute('poll', {})
    assert store.cursor(binding) == 0 and store.pending(binding) == []
    assert store.live(binding).active  # DB binding itself was never revoked.


@pytest.mark.asyncio
async def test_recent_thread_calls_and_message_refs_have_a_fixed_budget(native):
    store, binding, manager, client = native
    binding = store.save(replace(binding, thread_ids=(32, 33, 34, 35, 36)), expected_revision=1)
    client.binding = binding
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    recent_contract(manager, [])
    manager.responses['thread_recent'] = lambda args: {'ok': True, 'thread': {'id': args['thread_id']}, 'messages': [],
                                                       'pagination': {'has_more': False, 'next_before_id': None}}
    client.read_only = True
    result = await client.execute('poll', {'limit': 100})
    calls = [args for name, args in manager.calls if name.endswith('_thread_recent')]
    assert len(calls) == 4 and all(args['limit'] == 5 and 'agent' not in args for args in calls)
    assert result['recent_threads_omitted'] == 1


@pytest.mark.asyncio
async def test_recent_digest_or_thread_mismatch_is_not_cached(native):
    store, binding, manager, client = native
    manager.responses['inbox'] = {'ok': True, 'messages': []}
    recent_contract(manager, [{'id': 1500, 'thread_id': 32, 'body': 'Latest'}])
    manager.responses['thread_recent']['messages'][0]['body_digest'] = '0' * 64
    result = await client.execute('poll', {})
    assert result['recent_unavailable'] and not result['recent']
    assert store.pending(binding) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('repo', 'Other'), ('paths', ['other/file']), ('owner', 'other'), ('thread_id', 111)])
async def test_changed_live_task_authority_blocks_write_before_dispatch(native, field, value):
    store, binding, manager, client = native
    observed = {**task(), 'thread_id': 90}
    receipt = store.observe_task(binding, observed)
    store.confirm_task_delivery(binding, receipt)
    manager.responses['task']['task'] = {**observed, field: value}
    with pytest.raises(CoordinationError, match='reload'):
        await client.execute('checkpoint', {'task_id': 7, 'expected_revision': 0, 'payload': {'summary': 'Synthetic checkpoint'}})
    assert [name.rsplit('hub_agora_', 1)[-1] for name, _ in manager.calls] == ['task']


@pytest.mark.asyncio
async def test_new_submission_revision_does_not_refresh_old_review_vote(native):
    store, binding, manager, client = native
    observed = {**task(), 'owner': 'claude', 'thread_id': 90}
    receipt = store.observe_task(binding, observed, required_messages=[])
    store.confirm_task_delivery(binding, receipt)
    manager.responses['task']['task'] = {**observed, 'submission_revision': 3}
    manager.responses['task_review'] = {'ok': False, 'error': 'stale submission', 'status': 409}
    with pytest.raises(CoordinationError, match='stale submission'):
        await client.execute('review', {'task_id': 7, 'expected_submission_revision': 2, 'verdict': 'approve', 'body': 'Old vote'})
    assert manager.calls[-1][1]['expected_submission_revision'] == 2
    assert store.observed_task(binding, 7)['submission_revision'] == 2


@pytest.mark.asyncio
async def test_read_only_context_board_cannot_authorize_cross_repo_mutation(native):
    store, binding, manager, client = native
    foreign = {**task(), 'repo': 'Faustus', 'thread_id': 91}
    manager.responses['board']['tasks'].append(foreign)
    result = await client.execute('board', {})
    assert next(t for t in result['tasks'] if t['id'] == 7 and t['repo'] == 'Faustus')['read_only']
    receipt = store.observe_task(binding, foreign)
    store.confirm_task_delivery(binding, receipt)
    with pytest.raises(CoordinationError):
        await client.execute('post', {'thread_id': 91, 'body': 'Cross-repo publication is not granted'})
    assert len(manager.calls) == 1  # Only the authorized global board read.


@pytest.mark.asyncio
async def test_native_poll_compacts_and_persists_without_sync_or_ack(native):
    store, binding, manager, client = native
    manager.responses["inbox"] = {"ok": True, "messages": [
        {"id": 1, "thread_id": 32, "author": "claude", "body": "x" * 10000, "mentions": [binding.actor]},
        {"id": 2, "thread_id": 91, "author": "cursor", "body": "unrelated"}],
        "pending": [{"id": 500, "thread_id": 32, "author": "luis", "body": "an urgent mention", "mentions": [binding.actor]}]}
    first = await client.execute("poll", {})
    assert first["cursor"] == 2  # Pending mention500 must not skip unfetched3..499.
    assert {p["id"] for p in first["pending"]} == {1, 500}
    assert len(first["pending"][0]["body_excerpt"]) == 240
    assert len(json.dumps(first)) < 2000
    assert first["hub_acknowledged"] is False
    assert manager.calls[0] == ("mcp__hub-a__hub_agora_inbox", {"agent": binding.actor, "peek": True, "mine_only": False, "since_id": 0, "space_id":binding.space_id})
    manager.responses["inbox"] = {"ok": True, "messages": []}
    await client.execute("poll", {})
    assert manager.calls[2][1]["since_id"] == 2
    assert store.pending(binding)
    assert all(not name.endswith(("_sync", "_ack", "_heartbeat")) for name, _ in manager.calls)


@pytest.mark.asyncio
async def test_task_read_is_not_authority_until_native_delivery(native, monkeypatch):
    store, binding, manager, client = native
    manager.responses["task"] = {"ok": True, "task": task(), "messages": []}
    read = await client.execute("task", {"task_id": 7})
    with pytest.raises(CoordinationError, match="read this task"):
        await client.execute("checkpoint", {"task_id": 7, "expected_revision": 0, "payload": {"summary": "x"}})
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered(read)
    args = {"task_id": 7, "expected_revision": 0, "payload": {"summary": "x"}}
    assert grant.allows("coordination_checkpoint", args)
    await client.execute("checkpoint", args)
    assert manager.calls[-1][1] == {**args, "agent": binding.actor, "space_id":binding.space_id}


@pytest.mark.asyncio
async def test_detail_receipt_is_confirmed_only_by_delivery_hook(native, monkeypatch):
    store, binding, manager, client = native
    store.receive(binding, expected_cursor=0, next_cursor=1,
                  messages=[{"id": 1, "thread_id": 32, "body": "full detail"}])
    digest = store.pending(binding)[0]["digest"]
    fetched = await client.execute("detail", {"message_id": 1, "digest": digest})
    assert store.pending(binding)
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered({**fetched, "exit_code": 1})
    assert store.pending(binding)
    grant.delivered(fetched)
    assert store.pending(binding) == []
    assert manager.calls == []  # Stored detail read neither marks Hub seen nor sends a network call.


@pytest.mark.asyncio
async def test_unknown_write_outcome_is_not_retried_or_labeled_blocked(native):
    _, _, manager, client = native
    manager.responses["post"] = {"exit_code": 1, "outcome_unknown": True, "error": "lost reply"}
    with pytest.raises(UnknownOutcome):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_transport_failure_without_effect_receipt_cannot_authorize_write_retry(native):
    _, _, manager, client = native
    manager.responses["post"] = {"exit_code": 1, "stderr": "connection closed while waiting"}
    with pytest.raises(UnknownOutcome):
        await client.execute("post", {"thread_id": 32, "body": "publish once"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_binding_revoked_while_poll_awaits_does_not_advance_cursor(native):
    store, binding, manager, client = native
    manager.responses["inbox"] = {"ok": True, "messages": [{"id": 1, "thread_id": 32, "body": "x"}]}
    manager.after_call = lambda: store.save(replace(binding, active=False), expected_revision=1)
    with pytest.raises(StaleBinding):
        await client.execute("poll", {})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM details").fetchone()[0] == 0
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_disabled_and_denied_underlying_connector_tools_cannot_be_wrapped(native):
    _, _, manager, client = native
    client.disabled_tools = {"hub_agora_post"}
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    client.disabled_tools = set()
    client.tool_policy = type("Policy", (), {"blocks": lambda self, name: name.endswith("_post")})()
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert manager.calls == []


@pytest.mark.asyncio
async def test_read_only_native_client_rejects_housekeeping_writes_before_network(native):
    _, _, manager, client = native
    client.read_only = True
    with pytest.raises(CoordinationError):
        await client.execute("heartbeat", {})
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert manager.calls == []


def test_native_schemas_registry_and_capabilities_match():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_authority import AUTHORITY
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    schemas = {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}
    for name, action in NAMES.items():
        assert name in TOOL_HANDLERS and name in TOOL_TAGS and name in schemas
        spec = schemas[name]["function"]
        assert spec["parameters"]["additionalProperties"] is False
        assert not {"agent", "force", "exempt", "connector_id"} & set(spec["parameters"]["properties"])
        assert AUTHORITY.resolve(name).tool.canonical_id == f"coordination.{action}"
        caps = capabilities_for_tool(name)
        assert caps.known
        assert ToolEffect.USER_INTERACTION not in caps.effects
        assert (ToolEffect.WRITE_PRIVATE in caps.effects) == (action not in {"poll", "detail", "task", "board"})


def test_native_api_rejects_authority_fields_and_preserves_delivery_preconditions():
    from routes.coordination_routes import Action, Delivery, SaveBinding
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SaveBinding(connector_id="hub-a", owner="other")
    with pytest.raises(ValidationError):
        SaveBinding(connector_id="hub-a", expected_revision=True)
    with pytest.raises(ValidationError):
        Action(action="force")
    with pytest.raises(ValidationError):
        Action(action="poll", read_only="false")
    with pytest.raises(ValidationError):
        Delivery(receipt="r", binding_revision=True)


@pytest.mark.asyncio
async def test_write_revoked_after_dispatch_is_unknown_not_blocked(native):
    store, binding, manager, client = native
    manager.after_call = lambda: store.save(replace(binding, active=False), expected_revision=1)
    with pytest.raises(UnknownOutcome, match="may have completed"):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_lock_cannot_be_attached_to_a_foreign_task(native):
    store, binding, manager, client = native
    receipt = store.observe_task(binding, {**task(), "owner": "claude"})
    store.confirm_task_delivery(binding, receipt)
    with pytest.raises(CoordinationError, match="not owned"):
        await client.execute("lock", {"task_id": 7, "resources": ["path:Writer/src"]})
    assert not manager.calls


@pytest.mark.asyncio
async def test_review_needs_full_submission_discussion_not_a_task_summary(native, monkeypatch):
    store, binding, manager, client = native
    manager.responses["task"] = {"ok": True, "task": {**task(), "owner": "claude"},
        "messages": [{"id": 77, "thread_id": 90, "body": "Exact submitted work, evidence and limits", "author": "claude"}]}
    read = await client.execute("task", {"task_id": 7})
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered(read)
    vote = {"task_id": 7, "expected_submission_revision": 2, "verdict": "approve", "body": "reviewed"}
    with pytest.raises(CoordinationError, match="complete task discussion"):
        await client.execute("review", vote)
    assert not grant.allows("coordination_review", vote)
    pending = read["pending"][0]
    excerpt = await client.execute("detail", {"message_id": 77, "digest": pending["digest"], "length": 4})
    grant.delivered(excerpt)
    assert not grant.allows("coordination_review", vote)
    whole = await client.execute("detail", {"message_id": 77, "digest": pending["digest"], "length": 4000})
    grant.delivered(whole)
    assert grant.allows("coordination_review", vote)
    await client.execute("review", vote)
    assert manager.calls[-1][1]["expected_submission_revision"] == 2
