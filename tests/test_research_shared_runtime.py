import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import research_handler as shared
from services.research.research_handler import ResearchHandler
from services.research.service import ResearchService
from src.research_citations import SourceRegistry


def handler():
    result = ResearchHandler.__new__(ResearchHandler)
    result._active_tasks = {}
    result._legacy_engine = None
    return result


@pytest.mark.parametrize('session_id', ['../escape', '..', 'x/y', 'x\\y', ''])
def test_service_inherits_confined_reads_and_writes(tmp_path, monkeypatch, session_id):
    monkeypatch.setattr(shared, 'RESEARCH_DATA_DIR', tmp_path / 'research')
    outside = tmp_path / 'escape.json'
    outside.write_text('{"result":"private"}', encoding='utf-8')
    service = handler()
    assert service.get_status(session_id) is None
    assert service.get_result(session_id) is None
    assert service.get_sources(session_id) is None
    service.clear_result(session_id)
    service._save_result(session_id, {})
    with pytest.raises(ValueError):
        service.start_research(session_id, 'topic', 'unused', 'model')
    assert json.loads(outside.read_text(encoding='utf-8')) == {'result': 'private'}


def test_service_persists_owner_citations_and_keeps_consumed_report(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, 'RESEARCH_DATA_DIR', tmp_path)
    citations = SourceRegistry()
    citations.add({'url': 'https://example.test/a', 'summary': 'Useful evidence'})
    researcher = SimpleNamespace(citations=citations, findings=[citations.source(1)], report_language='es')
    service = handler()
    service._save_result('rp-shared', {'query': 'Tema', 'status': 'done', 'result': 'Informe [1]',
                                     'owner': 'alice', 'started_at': 1, 'researcher': researcher})
    saved = service._get_session_json('rp-shared', owner='alice')
    assert saved['report_language'] == 'es'
    assert saved['citation_registry'] == citations.snapshot()
    assert service._get_session_json('rp-shared', owner='bob') is None
    service.clear_result('rp-shared')
    assert service._get_session_json('rp-shared')['consumed'] is True
    assert service._get_session_json('rp-shared')['result'] == 'Informe [1]'


def test_service_rich_formatter_retains_sources_and_discards_malformed_rows():
    researcher = SimpleNamespace(findings=[None, 'bad', {'url': 'https://example.test',
        'title': 'Evidence', 'summary': 'Useful factual evidence'}], evolving_report='body',
        analyzed_urls=[42, {'url': 'https://example.test/other', 'title': 'Inspected'}])
    report = handler()._format_completed_report('query', '<think>private</think>Body', {}, 1, researcher)
    assert 'private' not in report
    assert 'Body' in report
    assert '### Analyzed URLs' in report
    assert 'https://example.test/other' in report
    assert [s.url for s in ResearchService._parse_sources(report)] == ['https://example.test']


def test_service_legacy_headers_position_and_owner_forwarded(monkeypatch):
    calls = []
    monkeypatch.setattr(shared.ResearchHandler, 'start_research',
        lambda *args, **kwargs: calls.append((args, kwargs)) or {'status': 'running'})
    handler().start_research('rp-test', 'topic', 'unused', 'model', 120, {'X-Test': '1'}, owner='alice')
    assert calls[0][1] == {'max_time': 120, 'llm_headers': {'X-Test': '1'}, 'owner': 'alice'}


def test_shared_engine_reaches_service_formatter_and_continuation(monkeypatch):
    service = handler()
    monkeypatch.setattr(service, '_probe_endpoint', AsyncMock())
    citations = SourceRegistry()
    citations.add({'url': 'https://example.test/a', 'summary': 'Useful evidence'})
    snapshot = citations.snapshot()
    researcher = SimpleNamespace(findings=[citations.source(1)], evolving_report='body',
        analyzed_urls=[], get_stats=lambda: {'Rounds': 1}, research=AsyncMock(return_value='Body [1]'))
    monkeypatch.setattr('src.deep_research.DeepResearcher', lambda **kwargs: researcher)
    report = asyncio.run(service.call_research_service('query', 'unused', 'model', prior_citations=snapshot))
    assert researcher.research.call_args.kwargs['prior_citations'] == snapshot
    assert [s.url for s in ResearchService._parse_sources(report)] == ['https://example.test/a']


@pytest.mark.parametrize('reply', ['Sí', 'vale', 'adelante', 'hazlo', 'dale', 'continúa', 'sigue', 'sí por favor'])
def test_spanish_affirmation_preserves_research_topic(monkeypatch, reply):
    monkeypatch.setattr('src.llm_core.llm_call_async', AsyncMock(side_effect=RuntimeError('offline')))
    session = SimpleNamespace(history=[SimpleNamespace(role='user', content='Compara motores de imagen'),
                                       SimpleNamespace(role='assistant', content='¿Continúo?')])
    result = asyncio.run(handler().synthesize_query(session, reply, 'unused', 'model'))
    assert result == 'Compara motores de imagen'
