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
