import json
from dataclasses import replace
import pytest
from src import agent_runners as reg, external_worker as worker


def test_codex_progress_answer_usage_and_exact_continuation():
    stream = worker._CodexStream()
    events = [
        {'type': 'thread.started', 'thread_id': 'thread-123'}, {'type': 'turn.started'},
        {'type': 'item.started', 'item': {'id': 'cmd1', 'type': 'command_execution', 'command': 'git status'}},
        {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private reasoning'}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Respuesta en español.'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 4, 'token': 'not returned'}},
    ]
    text = ''.join(stream.feed(json.dumps(event)) for event in events)
    assert 'git status' in text and 'Respuesta en español.' in text
    assert 'private reasoning' not in text
    assert stream.session_id == 'thread-123'
    assert stream.result['usage'] == {'input_tokens': 20, 'output_tokens': 4}
    runner = reg.get('codex', help_source='')
    assert reg.build_argv(runner, 'secret prompt', session=stream.session_id) == ['codex', 'exec', 'resume', 'thread-123', '--json', '-']


def test_codex_terminal_failure_is_distinct_from_retry_notice():
    stream = worker._CodexStream()
    assert '429' in stream.feed(json.dumps({'type': 'error', 'message': '429 retrying'}))
    assert not stream.result
    stream.feed(json.dumps({'type': 'turn.failed', 'error': {'message': 'quota exhausted'}}))
    assert stream.result['is_error'] is True


@pytest.mark.parametrize('field', ['resume', 'model'])
def test_cli_identifiers_cannot_inject_options(monkeypatch, tmp_path, field):
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    monkeypatch.setattr(worker, '_spawn', lambda *a, **k: pytest.fail('must not start'))
    result = worker.run_task(reg.get('codex', help_source=''), 'task', workspace=str(tmp_path),
                             **{field: '--dangerously-bypass-approvals-and-sandbox'})
    assert not result['ok'] and f'Invalid {field}' in result['error']


def test_codex_protocol_failure_is_not_success_even_with_zero_exit(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    script = tmp_path / 'fake_codex.py'
    script.write_text('import json\nprint(json.dumps({"type":"turn.failed","error":{"message":"quota exhausted"}}))', encoding='utf-8')
    runner = replace(reg.get('codex', help_source=''), argv=(sys.executable, str(script), '--json'))
    result = worker.run_task(runner, 'task', workspace=str(tmp_path))
    assert result['exit_code'] == 0 and not result['ok'] and result['status'] == 'error'
    assert result['unguarded'] is True  # Parsing events is not an enforcement gate.
