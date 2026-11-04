"""Conservative source authority (src/source_authority.py) and where it is used."""
import asyncio
import json

import pytest

from src import source_authority as sa
from src.research_citations import SourceRegistry


@pytest.mark.parametrize("url,kind,official", [
    ("https://www.nasa.gov/missions", "gov", True),
    ("https://www.defense.mil/x", "gov", True),
    ("https://www.boe.gob.es/diario_boe", "gov", True),
    ("https://www.gov.uk/guidance", "gov", True),
    ("https://www.legifrance.gouv.fr/", "gov", True),
    ("https://web.mit.edu/page", "edu", True),
    ("https://www.ox.ac.uk/", "edu", True),
    ("https://europa.eu/youreurope", "intergovernmental", True),
    ("https://www.who.int/news", "intergovernmental", True),
    ("https://docs.python.org/3/", "docs", True),
    ("https://developer.mozilla.org/en-US/", "docs", True),
    ("https://arxiv.org/abs/2610.07309", "paper", False),
    ("https://github.com/langgenius/dify", "code", False),
    ("https://en.wikipedia.org/wiki/Cat", "wiki", False),
    ("https://stackoverflow.com/q/1", "qa", False),
    ("https://www.reddit.com/r/x", "forum", False),
    ("https://news.ycombinator.com/item?id=1", "forum", False),
    ("https://medium.com/@a/b", "blog", False),
    ("https://www.amazon.es/dp/1", "shop", False),
    ("https://x.com/a/status/1", "social", False),
    ("https://www.youtube.com/watch?v=1", "video", False),
    ("https://example.com/", "unknown", False),
    ("not a url", "unknown", False),
    ("", "unknown", False),
])
def test_classification(url, kind, official):
    got = sa.classify_source(url)
    assert (got["source_type"], got["official"]) == (kind, official)


def test_lookalikes_are_never_official():
    # The signal must be the real suffix of the host, not a word inside it.
    for url in ("https://gov.example.com/", "https://nasa.gov.evil.com/", "https://edu-courses.com/",
                "https://mygov.com/", "https://docs-fake.com/", "https://example.com/docs.python.org",
                "https://forum.example.com/x"):
        assert sa.classify_source(url)["official"] is False, url


def test_open_platforms_are_never_official_even_when_they_look_serious():
    for url in ("https://github.com/anthropics/x", "https://huggingface.co/x", "https://pypi.org/project/x",
                "https://en.wikipedia.org/", "https://www.linkedin.com/in/x", "https://blog.example.gov.uk.evil.io/"):
        assert sa.classify_source(url)["official"] is False, url


def test_summarise_counts_types_and_official():
    out = sa.summarise(["https://www.nasa.gov/a", "https://reddit.com/r/a", {"url": "https://mit.edu/x"}, "junk"])
    assert out["total"] == 4 and out["official"] == 2
    assert out["by_type"] == {"gov": 1, "forum": 1, "edu": 1, "unknown": 1}


def test_stamp_keeps_a_real_type_already_set():
    entry = {"url": "https://www.nasa.gov/a", "source_type": "press"}
    assert sa.stamp(entry) == ("press", True)
    entry = {"url": "https://www.nasa.gov/a"}
    assert sa.stamp(entry) == ("gov", True)


def test_registry_entries_carry_the_classified_type():
    reg = SourceRegistry()
    n = reg.add({"url": "https://www.nasa.gov/a", "title": "t", "summary": "s"})
    assert reg._entries[n - 1]["source_type"] == "gov"
    n2 = reg.add({"url": "https://example.com/b", "title": "t", "summary": "s", "source_type": "press"})
    assert reg._entries[n2 - 1]["source_type"] == "press"


def test_research_handler_stamps_extracted_sources():
    from src.research_handler import ResearchHandler
    out = ResearchHandler._extract_sources([
        {"url": "https://www.nasa.gov/a", "title": "NASA", "summary": "A long enough summary of the page " * 3},
        {"url": "https://www.reddit.com/r/a", "title": "R", "summary": "Another long enough summary here " * 3},
    ])
    by_url = {s["url"]: s for s in out}
    assert by_url["https://www.nasa.gov/a"]["official"] is True
    assert by_url["https://www.reddit.com/r/a"]["official"] is False
    assert by_url["https://www.reddit.com/r/a"]["source_type"] == "forum"


def test_mcp_tool_classifies_a_list():
    from mcp_servers import source_authority_server as srv
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["source_authority"]
    res = asyncio.run(srv.call_tool("source_authority", {"urls": ["https://www.who.int/x", "https://medium.com/a"]}))
    data = json.loads(res[0].text)
    assert [(s["source_type"], s["official"]) for s in data["sources"]] == [("intergovernmental", True), ("blog", False)]
    assert data["summary"]["official"] == 1
    bad = asyncio.run(srv.call_tool("source_authority", {}))
    assert "needs urls" in bad[0].text


def test_builtin_server_is_registered():
    from src import builtin_mcp
    script, _name = builtin_mcp._BUILTIN_SERVERS["source_authority"]
    assert script == "mcp_servers/source_authority_server.py"
