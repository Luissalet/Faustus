import importlib
import pytest
from bs4 import BeautifulSoup


@pytest.mark.parametrize('module', ['src.research_handler', 'services.research.research_handler'])
def test_markdown_report_shows_available_citation_metrics(module):
    cls = importlib.import_module(module).ResearchHandler
    handler = object.__new__(cls)
    report = handler._format_research_report('query', 'body', {'Citations': '2 of 3 sources', 'Claims cited': '67%'}, 1)
    assert '**Citations:** 2 of 3 sources' in report
    assert '**Claims cited:** 67%' in report
    report = handler._format_research_report('query', 'body', {}, 1)
    assert '**Citations:**' not in report and '**Claims cited:**' not in report


def test_visual_report_citation_metrics_are_escaped_and_optional():
    from src.visual_report import generate_visual_report
    result = generate_visual_report('Query', '# Report', stats={'Citations': '<script>bad</script>', 'Claims cited': '67%'})
    stats = BeautifulSoup(result, 'html.parser').select_one('.stats-bar')
    assert stats.find('script') is None
    assert '<script>bad</script>' in stats.get_text()
    assert '67%' in stats.get_text() and 'Claims cited' in stats.get_text()
