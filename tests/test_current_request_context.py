"""Current request preservation uses the actual candidate preparation function."""
import ast
import asyncio
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading

from fastapi import HTTPException
import pytest

from src import context_budget as budget
from src import context_compactor as compactor
from src import llm_core
from src import model_context


@pytest.fixture(autouse=True)
def native_openai(monkeypatch):
    monkeypatch.setattr(llm_core, '_is_self_hosted_openai_compatible', lambda url: True)
    monkeypatch.setattr(llm_core, '_is_local_ollama_target', lambda url: False)
    monkeypatch.setattr(llm_core, 'is_sparks_url', lambda url: True)
    monkeypatch.setattr(compactor, 'get_setting', lambda key, default=None: default)


def prepare(monkeypatch, messages, *, window=1048576, configured=6000,
            output=4096, tools=(), turn=None):
    from src import agent_loop
    tree = ast.parse(Path(agent_loop.__file__).read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == '_trim_route_request_messages')
    namespace = {
        '_history_for_candidate': lambda items, *args: items,
        '_route_context_lengths': {}, '_turn_input_budget': turn,
        'context_length': window, 'max_tokens': output,
        'get_setting': lambda key, default=None: {'agent_input_token_budget': configured,
                                                 'agent_input_token_hard_max': 200000}.get(key, default),
        'estimate_tokens': model_context.estimate_tokens,
        'logger': logging.getLogger(__name__),
        'CurrentRequestCapacityError': budget.CurrentRequestCapacityError,
    }
    monkeypatch.setattr(model_context, 'budget_context_for_model', lambda *args, **kwargs: window)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'actual-agent-preparation', 'exec'), namespace)
    original = copy.deepcopy(messages)
    result = namespace[node.name]('http://127.0.0.1:1/v1', 'fixture-model', messages, list(tools))
    assert messages == original
    return result


def test_auto_preserves_long_current_with_resident_system_and_drops_old_history(monkeypatch):
    current = 'current-data-' * 60000
    resident = 'resident instructions\n' * 300
    messages = [{'role': 'system', 'content': resident},
                {'role': 'user', 'content': 'old-' * 50000},
                {'role': 'assistant', 'content': 'old answer ' * 30000},
                {'role': 'user', 'content': current},
                {'role': 'user', '_harness_note': True, 'content': 'Check the result.'}]
    result = prepare(monkeypatch, messages)
    assert result[-2]['content'] == current
    assert result[-1]['content'] == 'Check the result.'
    assert result[0]['content'] == resident
    assert len(result) == 3
    assert hashlib.sha256(result[-2]['content'].encode()).digest() == hashlib.sha256(current.encode()).digest()


def test_small_request_keeps_auto_history_target(monkeypatch):
    messages = [{'role': 'user', 'content': 'old' * 400000},
                {'role': 'assistant', 'content': 'old answer'},
                {'role': 'user', 'content': 'hello'}]
    result = prepare(monkeypatch, messages)
    assert result[-1]['content'] == 'hello'
    assert model_context.estimate_tokens(result) <= 200000
    assert budget.compute_input_token_budget(6000, 1048576, False) == 200000


@pytest.mark.parametrize('window,configured,output,tools', [
    (131072, 6000, 4096, []),
    (1048576, 32000, 4096, []),
    (1048576, 6000, 900000, []),
    (300000, 6000, 4096, [{'type': 'function', 'function': {'name': 'real_tool', 'description': 'schema-' * 40000}}]),
])
def test_insufficient_candidate_or_explicit_cap_is_visible(monkeypatch, window, configured, output, tools):
    with pytest.raises(HTTPException) as caught:
        prepare(monkeypatch, [{'role': 'user', 'content': 'current-' * 100000}],
                window=window, configured=configured, output=output, tools=tools)
    assert caught.value.status_code == 400
    assert 'intact current request' in caught.value.detail
    assert 'estimated' in caught.value.detail
    assert caught.value.fallback_eligible is True


def test_unknown_window_retains_existing_policy(monkeypatch):
    current = 'current-' * 100000
    result = prepare(monkeypatch, [{'role': 'user', 'content': current}], window=0)
    assert result[-1]['content'] != current
    assert model_context.estimate_tokens(result) <= 6000


def test_other_protocol_retains_existing_policy(monkeypatch):
    monkeypatch.setattr(llm_core, '_is_self_hosted_openai_compatible', lambda url: False)
    current = 'current-' * 100000
    result = prepare(monkeypatch, [{'role': 'user', 'content': current}])
    assert result[-1]['content'] != current
    assert model_context.estimate_tokens(result) <= 200000


@pytest.mark.parametrize('output', [0, -1])
def test_unlimited_output_has_no_invented_reserve(monkeypatch, output):
    reserves = llm_core.stream_context_reserves('http://127.0.0.1:1/v1', 'fixture', output, [])
    assert reserves['output_fields'] == {}
    assert reserves['output_reserve'] == 0
    assert reserves['output_reserve_known'] is False
    assert reserves['token_counts_estimated'] is True
    current = 'large-' * 120000
    result = prepare(monkeypatch, [{'role': 'user', 'content': current}], output=output)
    assert result[-1]['content'] == current


def test_none_uses_native_configured_fallback_and_positive_cap_is_exact(monkeypatch):
    monkeypatch.setattr(llm_core, '_local_sampler_default', lambda key, default: 12345 if key == 'local_openai_max_tokens_default' else default)
    url = 'http://127.0.0.1:1/v1'
    assert llm_core._openai_stream_output_fields(url, 'fixture', None) == {'max_tokens': 12345}
    assert llm_core.stream_context_reserves(url, 'fixture', None, [])['output_reserve'] == 12345
    assert llm_core.stream_context_reserves(url, 'fixture', 777, [])['output_reserve'] == 777


def test_explicit_and_unknown_budget_controls():
    assert budget.current_request_input_budget(200000, 1048576, 300000, output_reserve=32768, tool_reserve=1000, explicit=False) == 300000
    assert budget.current_request_input_budget(32000, 1048576, 1000, output_reserve=4096, tool_reserve=1000, explicit=True) == 32000
    assert budget.current_request_input_budget(6000, 0, 300000, output_reserve=0, tool_reserve=0, explicit=False) == 6000


def test_trimmer_opt_in_preserves_human_and_injected_tail_in_order():
    current = 'request-' * 200
    messages = [{'role': 'user', 'content': 'older-' * 300},
                {'role': 'assistant', 'content': 'history-' * 300},
                {'role': 'user', 'content': current},
                {'role': 'user', '_agent_injected': 'context', 'content': 'language note'},
                {'role': 'user', '_harness_note': True, 'content': 'runtime note'}]
    baseline = compactor.trim_for_context(copy.deepcopy(messages), 300, reserve_tokens=0)
    assert not any(m['content'] == current for m in baseline)
    preserved = compactor.trim_for_context(copy.deepcopy(messages), 600, reserve_tokens=0, preserve_current_request=True)
    assert [m['content'] for m in preserved] == [current, 'language note', 'runtime note']
    with pytest.raises(budget.CurrentRequestCapacityError):
        compactor.trim_for_context(messages, 100, reserve_tokens=0, preserve_current_request=True)


def test_candidate_budgets_are_independent(monkeypatch):
    current = 'candidate-' * 90000
    message = [{'role': 'user', 'content': current}]
    assert prepare(monkeypatch, message, window=1048576)[-1]['content'] == current
    with pytest.raises(HTTPException):
        prepare(monkeypatch, message, window=131072)
    assert message[0]['content'] == current


def test_anchor_reuses_native_human_request_provenance():
    messages = [{'role': 'user', 'content': 'human'},
                {'role': 'user', 'content': 'retrieved', 'metadata': {'trusted': False}},
                {'role': 'user', 'content': '[Runtime notice] more work'},
                {'role': 'user', '_agent_injected': 'context', 'content': 'language'},
                {'role': 'user', '_harness_note': True, 'content': 'continue'},
                {'role': 'user', 'content': 'tool image', 'metadata': {'source': 'tool result: screenshot'}}]
    assert budget.current_request_start(messages) == 0


def test_capacity_reason_survives_native_fallback_error_without_dispatch(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        prepare(monkeypatch, [{'role': 'user', 'content': 'current-' * 100000}], window=131072)
    error = caught.value
    def no_dispatch(*args, **kwargs):
        pytest.fail('Insufficient prepared context must not reach a backend')
    monkeypatch.setattr(llm_core, 'stream_llm', no_dispatch)
    async def factory(*args):
        raise error
    async def collect():
        return [chunk async for chunk in llm_core.stream_llm_with_fallback(
            [('http://127.0.0.1:1/v1', 'fixture-model', {})], [], candidate_request_factory=factory,
        )]
    chunks = asyncio.run(collect())
    payload = json.loads(chunks[0].split('data: ', 1)[1])
    assert payload['status'] == 400
    assert payload['error_class'] == 'context_length_exceeded'
    assert payload['request_preparation'] is True
    assert payload['retryable'] is False
    assert 'intact current request' in payload['error']
    ordinary = ValueError('PRIVATE SOURCE CONTENT')
    assert 'PRIVATE SOURCE CONTENT' not in llm_core._request_factory_error_chunk(ordinary, None)


@pytest.mark.parametrize('order', [('small', 'large'), ('large', 'small')])
def test_native_fallback_checks_each_window_without_replaying_impossible_candidate(monkeypatch, order):
    current = 'candidate-' * 90000
    original = [{'role': 'user', 'content': current}]
    schemas = {name: [{'type': 'function', 'function': {'name': f'{name}_tool',
               'description': name * (20 if name == 'large' else 200),
               'parameters': {'type': 'object', 'properties': {}}}}] for name in order}
    windows, outputs = {'small': 131072, 'large': 1048576}, {'small': 2048, 'large': 8192}
    checked, dispatched = [], []
    async def factory(index, url, model, headers):
        checked.append(model)
        messages = prepare(monkeypatch, original, window=windows[model],
                           output=outputs[model], tools=schemas[model])
        return {'messages': messages, 'kwargs': {'tools': schemas[model], 'max_tokens': outputs[model]}}
    async def backend(url, model, messages, **kwargs):
        dispatched.append((model, messages, kwargs))
        if order[0] == 'large':
            yield 'event: error\ndata: {"status":503,"error":"private fixture unavailable"}\n\n'
        else:
            yield 'data: {"delta":"private fixture answer"}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(llm_core, 'is_sparks_url', lambda url: False)
    monkeypatch.setattr(llm_core, 'stream_llm', backend)
    async def collect():
        return [chunk async for chunk in llm_core.stream_llm_with_fallback(
            [('http://127.0.0.1:1/v1', name, {}) for name in order], original,
            candidate_request_factory=factory, fallback_statuses={503},
        )]
    chunks = asyncio.run(collect())
    assert checked == list(order)
    assert [item[0] for item in dispatched] == ['large']
    assert dispatched[0][1][-1]['content'] == current
    assert dispatched[0][2]['tools'] == schemas['large']
    assert dispatched[0][2]['max_tokens'] == 8192
    assert original == [{'role': 'user', 'content': current}]
    if order[0] == 'small':
        assert any('private fixture answer' in chunk for chunk in chunks)
    else:
        assert any('context_length_exceeded' in chunk for chunk in chunks)


@pytest.mark.parametrize('output,expected', [(None, 12345), (0, None), (-1, None), (777, 777)])
def test_output_reserve_matches_real_private_http_stream(monkeypatch, output, expected):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body = (b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":1}}\n\n'
                    b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(llm_core, 'is_sparks_url', lambda url: False)
    monkeypatch.setattr(llm_core, '_model_load_defaults', lambda *args: {})
    monkeypatch.setattr(llm_core, '_local_sampler_default', lambda key, default: 12345 if key == 'local_openai_max_tokens_default' else default)
    monkeypatch.setattr(llm_core, '_is_host_dead', lambda *args: False)
    url = f'http://127.0.0.1:{server.server_port}/v1'
    messages = [{'role': 'user', 'content': 'Calculate the supplied ledger, then return JSON.'}]
    tools = [{'type': 'function', 'function': {'name': 'fixture_tool', 'description': 'Private fixture',
                                               'parameters': {'type': 'object', 'properties': {}}}}]
    reserves = llm_core.stream_context_reserves(url, 'fixture-model', output, tools, messages)
    async def collect():
        return [chunk async for chunk in llm_core._stream_llm_inner(
            url, 'fixture-model', messages, max_tokens=output, tools=tools, max_retries=1,
        )]
    try:
        chunks = asyncio.run(collect())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(captured) == 1
    assert captured[0].get('max_tokens') == expected
    assert reserves['output_reserve'] == (expected or 0)
    assert reserves['output_reserve_known'] is (expected is not None)
    assert captured[0]['tools'] == tools
    assert captured[0]['messages'] == messages
    assert any('done' in chunk for chunk in chunks)
    assert chunks[-1] == 'data: [DONE]\n\n'
