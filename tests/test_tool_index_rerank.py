from types import SimpleNamespace

import pytest

from src import tool_index as tools


def test_opt_in_is_owned_and_strict(monkeypatch):
    calls = []
    def setting(key, owner, default):
        calls.append((key, owner, default))
        return True
    monkeypatch.setattr('src.settings.get_user_setting', setting)
    assert tools.tool_rerank_options(None) == {}
    assert tools.tool_rerank_options(' ') == {}
    assert not calls
    assert tools.tool_rerank_options('alice') == {'owner': 'alice', 'rerank_tools': True}
    assert calls == [('agent_tool_rerank', 'alice', False)]
    monkeypatch.setattr('src.settings.get_user_setting', lambda *a: 'false')
    assert tools.tool_rerank_options('alice') == {}


def test_actual_preferences_keep_reranker_choice_per_user(monkeypatch):
    from src import settings
    from src.rerank import _settings_choice
    monkeypatch.setattr(settings, 'load_settings', lambda: {})
    monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr('routes.prefs_routes._load_for_user', lambda owner: {
        'agent_tool_rerank': owner == 'alice', 'rerank_endpoint_id': owner + '-endpoint',
        'rerank_model': owner + '-model'})
    assert tools.tool_rerank_options('alice')['owner'] == 'alice'
    assert tools.tool_rerank_options('bob') == {}
    assert _settings_choice('alice') == ('alice-endpoint', 'alice-model')
    assert _settings_choice('bob') == ('bob-endpoint', 'bob-model')


def test_reranker_only_sees_public_descriptions_and_owner(monkeypatch):
    names = list(tools.BUILTIN_TOOL_DESCRIPTIONS)[:3]
    calls = []
    def rerank(query, passages, **options):
        calls.append((query, passages, options))
        return SimpleNamespace(reranked=True, passages=list(reversed(passages)))
    monkeypatch.setattr('src.rerank.rerank', rerank)
    result = tools.rerank_candidates('my request', [names[0], 'private_connector', *names[1:]], k=2, owner='alice')
    assert result == [names[-1], 'private_connector']
    query, passages, options = calls[0]
    assert query == 'my request'
    assert [p['id'] for p in passages] == names
    assert all(p['text'] == tools.BUILTIN_TOOL_DESCRIPTIONS[p['id']] for p in passages)
    assert options == {'owner': 'alice', 'head': 32, 'timeout': 2.0}


@pytest.mark.parametrize('reason', ['timeout', 'no_reranker_configured', 'endpoint_unreachable', 'bad_response'])
def test_failure_keeps_exact_original_selection(monkeypatch, reason):
    names = list(tools.BUILTIN_TOOL_DESCRIPTIONS)[:4]
    monkeypatch.setattr('src.rerank.rerank', lambda *a, **k: SimpleNamespace(reranked=False, reason=reason))
    assert tools.rerank_candidates('query', names, k=2, owner='alice') == names[:2]


def test_no_owner_never_calls_reranker(monkeypatch):
    def unexpected(*a, **k):
        pytest.fail('unowned network request')
    monkeypatch.setattr('src.rerank.rerank', unexpected)
    names = list(tools.BUILTIN_TOOL_DESCRIPTIONS)[:4]
    assert tools.rerank_candidates('query', names, k=2, owner='') == names[:2]


def test_invalid_reranker_result_cannot_introduce_tools(monkeypatch):
    names = list(tools.BUILTIN_TOOL_DESCRIPTIONS)[:4]
    monkeypatch.setattr('src.rerank.rerank', lambda *a, **k: SimpleNamespace(
        reranked=True, passages=[{'id': 'injected-tool'}]))
    assert tools.rerank_candidates('query', names, k=2, owner='alice') == names[:2]


def test_selection_expands_candidates_and_preserves_mandatory_tools(monkeypatch):
    index = tools.ToolIndex.__new__(tools.ToolIndex)
    names = list(tools.BUILTIN_TOOL_DESCRIPTIONS)[:12]
    calls = []
    index.retrieve = lambda query, k=8: calls.append(k) or names[:k]
    monkeypatch.setattr('src.rerank.rerank', lambda query, passages, **options: SimpleNamespace(
        reranked=True, passages=list(reversed(passages))))
    selected = index.get_tools_for_query('xyz', k=2, owner='alice', rerank_tools=True)
    assert calls == [8]
    assert set(names[6:8]).issubset(selected)
    assert tools.ALWAYS_AVAILABLE.issubset(selected)
    calls.clear()
    selected = index.get_tools_for_query('xyz', k=2)
    assert calls == [2]
    assert set(names[:2]).issubset(selected)
