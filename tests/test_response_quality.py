"""Native reply renderer and common answer rules, without loading a model."""
import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_response_render():
    if not shutil.which('node') or not (ROOT / 'node_modules/esbuild').exists():
        pytest.skip('node and esbuild needed')
    result = subprocess.run(['node', 'studio/checks/response-quality.check.mjs'], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ALL OK' in result.stdout


def test_both_tool_protocols_receive_answer_quality_rules():
    tree = ast.parse((ROOT / 'src/agent_loop.py').read_text(encoding='utf-8'))
    env = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {
            '_RESPONSE_QUALITY_RULES', '_AGENT_RULES', '_API_AGENT_RULES'
        } for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<rules>', 'exec'), env)
    for key in ('_AGENT_RULES', '_API_AGENT_RULES'):
        assert 'a successful search is evidence to answer from' in env[key]
        assert 'never invent' in env[key].lower()
        assert 'details fence' in env[key]
        assert 'exact output requests' in env[key]
        assert 'unsourced benchmarks' in env[key]
        assert 'Otherwise reply with one short confirmation' not in env[key]


def test_search_then_fetch_sources_survive_history():
    from src.response_quality import merge_web_sources
    first = [{'url': 'https://example.com/a', 'title': 'First'}]
    second = [{'url': 'https://example.com/a', 'title': 'Duplicate'},
              {'url': 'https://example.com/b', 'title': 'Second'}]
    assert merge_web_sources(first, second) == first + [second[1]]
    assert merge_web_sources(first, None) == first
    assert merge_web_sources(first, [None, {}, {'url': []}]) == first
    assert first == [{'url': 'https://example.com/a', 'title': 'First'}]


@pytest.mark.asyncio
async def test_direct_fetch_exposes_observed_source(monkeypatch):
    from src.agent_tools.web_tools import WebFetchTool
    monkeypatch.setattr('src.search.content.fetch_webpage_content', lambda *a, **kw:
                        {'success': True, 'title': 'Read page', 'content': 'Observed values 12 and 18.'})
    result = await WebFetchTool().execute('https://example.com/read', {'owner': 'test'})
    assert result['exit_code'] == 0, result
    assert result['web_sources'] == [{'url': 'https://example.com/read', 'title': 'Read page'}]
    assert 'Observed values 12 and 18.' in result['output']


@pytest.mark.parametrize('text', [
    'Analiza estos datos de prueba: día1=120000, día4=45000 y día14=16000. ¿Ha fracasado?',
    'Compare this test data: first=12, last=18. Is that enough to conclude success?',
    'Analyze this sample data and explain its limitations.',
])
def test_sample_data_analysis_does_not_require_workspace_mutation(text):
    from src.agent_loop import _looks_like_workspace_coding_request
    assert not _looks_like_workspace_coding_request(text)


@pytest.mark.parametrize('text', [
    'Prueba la app con datos de prueba.',
    'Crea datos de prueba en fixtures.py.',
    'Fix the failing test using this test data.',
])
def test_real_test_work_with_sample_data_still_requires_tools(text):
    from src.agent_loop import _looks_like_workspace_coding_request
    assert _looks_like_workspace_coding_request(text)


def test_real_linked_workspace_analysis_does_not_nudge(tmp_path, monkeypatch):
    from tests.test_agent_harness_loop_partial_work import _patch, _run, _workspace
    workspace = _workspace(tmp_path)
    calls = _patch(monkeypatch, [('Los valores son12y18; faltan los intermedios.', 'stop')], workspace)
    events = _run(workspace, user='Analiza estos datos de prueba: 12 y 18. Explica qué sabemos y qué falta.')
    assert calls['n'] == 1
    assert not any(e.get('reason') == 'no_workspace_action' for e in events)


def test_completed_tool_progress_is_retained_separately_from_final_answer(tmp_path, monkeypatch):
    from tests.test_agent_harness_loop_partial_work import _patch, _run, _workspace, EDIT_CART
    workspace = _workspace(tmp_path)
    progress = 'Compruebo y actualizo el cálculo.'
    _patch(monkeypatch, [(progress + '\n\n' + EDIT_CART, 'tool_calls'),
                        ('Resultado verificado en cart.py.', 'stop')], workspace)
    events = _run(workspace, user='Implementa total_con_envio en cart.py y guarda el cambio.')
    final = ''
    for event in events:
        if event.get('type') == 'response_replace':
            final = event.get('text', '')
        elif 'delta' in event and not event.get('thinking'):
            final += event['delta']
    assert progress not in final
    assert 'Resultado verificado' in final
    assert any(event.get('type') == 'response_replace' for event in events)
    assert 'def total_con_envio' in Path(workspace, 'cart.py').read_text(encoding='utf-8')


def test_source_list_ignores_search_navigation_urls():
    from src.agent_loop import _enrich_web_sources
    sources = _enrich_web_sources([{'url': '/clev?tracking=1', 'title': 'Navigation'},
                                   {'url': 'https://example.com/article', 'title': 'Article'}])
    assert [s['url'] for s in sources] == ['https://example.com/article']


@pytest.mark.parametrize('closing_value', [
    'Las fases dependen de la porción iluminada visible; la sombra terrestre causa eclipses.',
    '{"fase":"iluminacion"}', 'Done.', '', TimeoutError('slow model'),
])
def test_empty_length_after_search_gets_a_final_answer_with_same_generation_settings(monkeypatch, closing_value):
    from tests.test_agent_rounds_exhausted import _patch_common, _collect, _types
    import src.agent_loop as al
    import src.llm_core as llm_core
    _patch_common(monkeypatch)
    scripted = iter([('```web_search\nmoon phases\n```', 'tool_calls'), ('', 'length'), ('', 'length'), ('', 'length')])
    async def stream(*args, **kwargs):
        text, finish = next(scripted)
        if finish == 'tool_calls':
            yield 'data: ' + __import__('json').dumps({'type': 'tool_calls', 'calls': [
                {'name': 'web_search', 'arguments': '{"query":"moon phases"}'}]}) + '\n\n'
        elif text:
            yield 'data: ' + __import__('json').dumps({'delta': text}) + '\n\n'
        yield 'data: ' + __import__('json').dumps({'type': 'finish', 'finish_reason': finish}) + '\n\n'
    calls = []
    async def closing(**kwargs):
        calls.append(kwargs)
        if isinstance(closing_value, Exception):
            raise closing_value
        return closing_value
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    monkeypatch.setattr(al, '_agent_route_tool_mode', lambda *args, **kwargs: (True, False, False))
    monkeypatch.setattr(llm_core, 'llm_call_async', closing)
    overrides = {'num_ctx': 1048576, 'think': True, 'reasoning_effort': 'max'}
    events = _types(_collect(al.stream_agent_loop('http://x/v1', 'm',
        [{'role': 'user', 'content': '¿Las fases de la Luna son eclipses?'}],
        max_rounds=8, relevant_tools={'web_search'}, gen_overrides=overrides)))
    assert len(calls) == 1
    assert calls[0]['gen_overrides'] == overrides
    assert calls[0]['_spend_purpose'] == 'final_synthesis'
    assert 'output format' in calls[0]['messages'][-1]['content']
    text = ''
    for event in events:
        if event.get('type') == 'response_replace':
            text = event.get('text', '')
        elif 'delta' in event and not event.get('thinking'):
            text += event['delta']
    if closing_value in ('Done.', '') or isinstance(closing_value, Exception):
        assert 'incomplete' in text or 'incompleto' in text
        assert text.strip() != 'Done.'
    else:
        assert closing_value in text
        if closing_value.startswith('{'):
            assert __import__('json').loads(text) == {'fase': 'iluminacion'}


def test_nonempty_length_answer_does_not_get_replaced_by_final_synthesis(monkeypatch):
    from tests.test_agent_harness_loop_partial_work import _patch, _run
    import src.llm_core as llm_core
    calls = _patch(monkeypatch, [('', 'length'), ('', 'length'), ('La explicación parcial.', 'length')])
    async def unexpected(**kwargs):
        pytest.fail('nonempty partial answer must be preserved')
    monkeypatch.setattr(llm_core, 'llm_call_async', unexpected)
    events = _run(None, user='Explica las fases lunares.')
    assert calls['n'] == 3
    assert any('La explicación parcial.' in e.get('delta', '') for e in events)
