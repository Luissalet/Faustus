"""Independent tests of final-wire delivery, without model or Hub calls."""
import copy
import json

import pytest

from src.coordination.delivery import CoordinationAudit


class Grant:
    def __init__(self):
        self.received = []
        self.revoked = False

    def delivered(self, result):
        if self.revoked:
            raise ValueError('revoked')
        self.received.append(copy.deepcopy(result))


@pytest.fixture
def delivery():
    grant = Grant()
    output = json.dumps({'body': 'Complete synthetic detail', 'offset': 0, 'next_offset': None})
    rendered = f'### Native detail\n```\n{output}\n```'
    result = {'output': output, 'exit_code': 0, '_coordination_delivery': {'binding': 'b', 'revision': 1, 'receipt': 'first'}}
    record = {'tool_name': 'coordination_detail', 'call_id': 'c1', 'result': result, 'text': rendered}
    audit = CoordinationAudit(grant=grant)
    return audit, grant, record


def wire(audit, record, *, candidate=0, body=None, call_id='c1'):
    messages = [{'role': 'tool', 'tool_call_id': call_id, 'content': record['text'] if body is None else body}]
    audit.observe_round(2, messages, candidate=candidate)
    audit.observe_wire(2, {'messages': messages})


def test_execution_stage_and_prepared_payload_alone_do_not_ack(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    assert grant.received == []
    wire(audit, record)
    assert grant.received == []  # Failed HTTP/no model chunk must leave it pending.


def test_complete_wire_and_matching_candidate_ack_once(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record)
    audit.accepted(2)
    audit.accepted(2)
    assert len(grant.received) == 1


@pytest.mark.parametrize('transform', ['truncated', 'offload', 'other-tool', 'failed', 'missing-output'])
def test_stage_rejects_non_delivered_representations(delivery, transform):
    audit, grant, record = delivery
    if transform == 'truncated':
        record['text'] = record['text'][:20]
    elif transform == 'offload':
        record['text'] = 'Stored as artifact; preview only'
    elif transform == 'other-tool':
        record['tool_name'] = 'web_fetch'
    elif transform == 'failed':
        record['result']['exit_code'] = 1
    else:
        record['result'].pop('output')
    audit.stage([record])
    wire(audit, record)
    audit.accepted(2)
    assert grant.received == []


@pytest.mark.parametrize('body', ['', 'Preview only', '{"body":"partial"}'])
def test_final_wire_dropped_or_shortened_content_does_not_ack(delivery, body):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record, body=body)
    audit.accepted(2)
    assert grant.received == []


def test_wrong_native_call_id_cannot_confirm_another_result(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record, call_id='other')
    audit.accepted(2)
    assert grant.received == []


def test_wrong_candidate_or_round_cannot_ack(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record, candidate=1)
    audit.accepted(1, 1)
    audit.accepted(2, 0)
    assert grant.received == []
    audit.accepted(2, 1)
    assert len(grant.received) == 1


def test_latest_wire_replaces_earlier_complete_wire(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record)
    audit.observe_wire(2, {'messages': []})
    audit.accepted(2)
    assert grant.received == []


def test_latest_noncomparable_wire_invalidates_previous_delivery(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record)
    audit.observe_wire(2, {'input': 'A different protocol without the detail'})
    audit.accepted(2)
    assert grant.received == []


def test_receipt_snapshot_cannot_be_changed_after_staging(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    record['result']['_coordination_delivery']['receipt'] = 'UNSEEN-SECOND-MESSAGE'
    wire(audit, record)
    audit.accepted(2)
    assert len(grant.received) == 1
    assert grant.received[0]['_coordination_delivery']['receipt'] == 'first'


def test_revocation_after_wire_leaves_receipt_pending(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    wire(audit, record)
    grant.revoked = True
    audit.accepted(2)
    assert grant.received == [] and 'c1' in audit.pending


def test_lossless_fenced_tool_result_can_be_delivered(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    messages = [{'role': 'user', 'content': 'UNTRUSTED_SOURCE_DATA\n' + record['text'] + '\nEND_UNTRUSTED_SOURCE_DATA'}]
    audit.observe_round(2, messages)
    audit.observe_wire(2, {'messages': messages})
    audit.accepted(2)
    assert len(grant.received) == 1


def test_anthropic_native_tool_result_is_recognized(delivery):
    audit, grant, record = delivery
    audit.stage([record])
    messages = [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1', 'content': record['text']}]}]
    audit.observe_round(2, messages)
    audit.observe_wire(2, {'messages': messages})
    audit.accepted(2)
    assert len(grant.received) == 1


@pytest.mark.parametrize('mode', ['complete', 'wire-dropped', 'http-error', 'no-next-round'])
def test_real_loop_ack_waits_for_delivered_followup_request(monkeypatch, tmp_path, mode):
    import src.agent_loop as loop
    from src.coordination import runtime
    from src.coordination.bindings import Binding
    from src.context_engine.prompt_audit import observe_wire
    from tests.test_agent_harness_loop import _collect, _events, _patch_common

    grant = Grant()
    grant.binding = Binding(id='b', owner='alice', project_id='p', session_id='s', connector_id='hub',
                            actor='faustus-test', operations=('detail',))
    grant.allows = lambda *args: True
    _patch_common(monkeypatch)
    monkeypatch.setattr(runtime, 'resolve_grant', lambda *args, **kwargs: grant)
    output = json.dumps({'body': 'Complete synthetic detail', 'offset': 0, 'next_offset': None})
    result = {'output': output, 'exit_code': 0, '_coordination_delivery': {'binding': 'b', 'revision': 1, 'receipt': 'first'}}
    async def execute(block, *args, **kwargs):
        return block.tool_type, copy.deepcopy(result)
    monkeypatch.setattr(loop, 'execute_tool_block', execute)
    calls = []
    async def stream(candidates, messages, **kwargs):
        if calls and mode == 'no-next-round':
            # Round caps may be extended by the harness. This models an
            # interrupted/absent follow-up request, not an advisory round cap.
            yield 'data: [DONE]\n\n'
            return
        prepared = await kwargs['candidate_request_factory'](0, *candidates[0])
        outgoing = copy.deepcopy(prepared['messages'])
        calls.append(outgoing)
        if len(calls) > 1 and mode == 'wire-dropped':
            outgoing = [message for message in outgoing if message.get('role') not in {'tool'} and output not in str(message.get('content', ''))]
        observe_wire({'messages': outgoing})
        if len(calls) == 1:
            yield 'data: ' + json.dumps({'type': 'tool_calls', 'calls': [{'id': 'coord-read', 'name': 'coordination_detail', 'arguments': json.dumps({'message_id': 1, 'digest': '0' * 64})}]}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'finish', 'finish_reason': 'tool_calls'}) + '\n\n'
        elif mode == 'http-error':
            yield 'event: error\ndata: ' + json.dumps({'error': 'synthetic HTTP failure', 'status': 400}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({'delta': 'Done.', 'candidate_index': 0}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'finish', 'finish_reason': 'stop', 'candidate_index': 0}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', stream)
    events = _events(_collect(loop.stream_agent_loop('http://scripted/v1', 'scripted', [{'role': 'user', 'content': 'Read the pending coordination detail.'}],
        owner='alice', session_id='s', workspace=str(tmp_path), relevant_tools={'coordination_detail'},
        max_rounds=1 if mode == 'no-next-round' else 2,
        harness_options={'no_memory': True, 'no_skills': True, 'repo_map': False, 'review_model': 'off', 'project_id': 'p'})))
    assert any(event.get('type') == 'tool_output' and event.get('tool') == 'coordination_detail' for event in events), events
    assert len(grant.received) == (1 if mode == 'complete' else 0)
