import sys
from types import ModuleType
import pytest
from src.deep_research import DeepResearcher


def researcher():
    item = DeepResearcher.__new__(DeepResearcher)
    item._emit = lambda **kwargs: None
    item._active_search_provider = lambda: 'searxng'
    item.urls_fetched = set()
    item.max_content_chars = 12000
    item.extraction_timeout = 30
    item.max_report_tokens = 2048
    item.category = None
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['', '   ', '{"summary":"","evidence":""}', RuntimeError('local model unavailable')])
async def test_extraction_failure_keeps_attributed_source(monkeypatch, reply):
    source = ModuleType('src.search')
    source.fetch_webpage_content = lambda *args: {'success': True, 'content': 'Original evidence from the page. '*500, 'title': 'Original title'}
    monkeypatch.setitem(sys.modules, 'src.search', source)
    item = researcher()
    async def llm(*args, **kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply
    item._llm = llm
    result = await item._fetch_and_extract('https://example.test/source', 'question', '')
    assert result['url'] == 'https://example.test/source'
    assert result['title'] == 'Original title'
    assert result['extraction_mode'] == 'rendered_page_fallback'
    assert 0 < len(result['evidence']) <= 6000
    assert result['evidence'].startswith('Original evidence')
    formatted = item._numbered_findings([result])
    assert 'not a verified conclusion' in formatted
    assert len(formatted) < 2300


def test_synthesis_keeps_evidence_beside_the_summary():
    formatted = researcher()._format_findings([{'url': 'https://example.test', 'title': 'Source', 'summary': 'A conclusion', 'evidence': 'Measured source details'}])
    assert 'A conclusion' in formatted and 'Measured source details' in formatted


@pytest.mark.asyncio
async def test_empty_final_answer_keeps_previous_report():
    item = researcher()
    async def empty(*args, **kwargs): return ''
    item._llm = empty
    assert await item._final_report('question', 'Existing source-grounded report') == 'Existing source-grounded report'


def test_empty_page_cannot_become_a_finding():
    assert researcher()._rendered_page_finding('https://example.test', '', {}, '') is None
