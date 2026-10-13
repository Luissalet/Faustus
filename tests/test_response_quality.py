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


def test_researched_answer_gets_one_editorial_round_with_same_evidence(monkeypatch):
    from tests.test_agent_harness_loop_partial_work import _patch, _run
    from src import agent_loop
    search = '```web_search\n{"query":"two observations"}\n```'
    calls = _patch(monkeypatch, [(search, 'tool_calls'), ('Draft with an unsupported conclusion.', 'stop'),
                                ('Two observations are supported; the cause is unknown.', 'stop')])
    async def evidence(block, *args, **kwargs):
        return block.tool_type, {'output': 'Source https://example.com/data: observed values12and18.', 'exit_code': 0}
    monkeypatch.setattr(agent_loop, 'execute_tool_block', evidence)
    events = _run(None, user='Find these two observations and explain what they show.')
    final = ''
    for event in events:
        if event.get('type') == 'response_replace':
            final = event.get('text', '')
        elif 'delta' in event and not event.get('thinking'):
            final += event['delta']
    assert calls['n'] == 3
    assert 'unsupported conclusion' not in final
    assert 'cause is unknown' in final


def test_editorial_round_respects_explicit_round_budget(monkeypatch):
    from tests.test_agent_harness_loop_partial_work import _patch, _run
    from src import agent_loop
    search = '```web_search\n{"query":"data"}\n```'
    calls = _patch(monkeypatch, [(search, 'tool_calls'), ('Answer from the source.', 'stop')])
    async def evidence(block, *args, **kwargs):
        return block.tool_type, {'output': 'Source https://example.com/data: value12.', 'exit_code': 0}
    monkeypatch.setattr(agent_loop, 'execute_tool_block', evidence)
    events = _run(None, user='Find these data and summarize them.', max_rounds=2)
    assert calls['n'] == 2
    assert any('Answer from the source' in str(event.get('delta', '')) for event in events)
