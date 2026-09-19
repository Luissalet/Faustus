"""Tests for src/source_types.py (classify_source + extraction_quality) and
its wiring into services/search/ranking.py and services/search/content.py."""

import pytest

from src.source_types import classify_source, extraction_quality, quality_note
from services.search.ranking import rank_search_results


# ---------------------------------------------------------------------------
# classify_source: ~25 URLs -> expected source_type / is_official
# ---------------------------------------------------------------------------

CLASSIFY_TABLE = [
    ("https://www.whitehouse.gov/briefing", "official", True),
    ("https://www.gob.mx/presidencia", "official", True),
    ("https://www.gouv.fr/actualites", "official", True),
    ("https://www.army.mil/news", "official", True),
    ("https://www.un.int/statement", "official", True),
    ("https://docs.python.org/3/library/re.html", "docs", True),
    ("https://developer.mozilla.org/en-US/docs/Web", "docs", True),
    ("https://learn.microsoft.com/en-us/azure/", "docs", True),
    ("https://github.com/anthropics/claude-code", "code", False),
    ("https://pypi.org/project/requests/", "code", False),
    ("https://npmjs.com/package/react", "code", False),
    ("https://arxiv.org/abs/2401.00001", "academic", False),
    ("https://doi.org/10.1000/xyz123", "academic", False),
    ("https://pubmed.ncbi.nlm.nih.gov/12345678/", "academic", False),
    ("https://mit.edu/research/paper", "academic", False),
    ("https://www.imperial.ac.uk/study", "academic", False),
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "reference", False),
    ("https://www.britannica.com/topic/python", "reference", False),
    ("https://stackoverflow.com/questions/123/how-to-x", "forum", False),
    ("https://www.reddit.com/r/python/comments/abc", "forum", False),
    ("https://python.stackexchange.com/questions/1", "forum", False),
    ("https://www.linkedin.com/in/someone", "social", False),
    ("https://x.com/someuser/status/123", "social", False),
    ("https://www.amazon.com/dp/B08N5WRWNW", "shop", False),
    ("https://example.com/product/widget-42", "shop", False),
    ("https://medium.com/@author/a-post-about-python-abc123", "blog", False),
    ("https://example.com/blog/2026/great-post", "blog", False),
    ("https://apnews.com/hub/technology", "news", False),
    ("https://www.bbc.com/news/world", "news", False),
    ("https://some-random-personal-site.example/page", "unknown", False),
]


@pytest.mark.parametrize("url,expected_type,expected_official", CLASSIFY_TABLE)
def test_classify_source_table(url, expected_type, expected_official):
    result = classify_source(url, title="")
    assert result["source_type"] == expected_type, (url, result)
    assert result["is_official"] is expected_official, (url, result)


def test_classify_source_handles_garbage_url_without_raising():
    result = classify_source("not a url at all", title="")
    assert result["source_type"] == "unknown"
    assert result["is_official"] is False


def test_classify_source_handles_empty_url():
    result = classify_source("", title="")
    assert result["source_type"] == "unknown"
    assert result["is_official"] is False


# ---------------------------------------------------------------------------
# extraction_quality: synthetic texts
# ---------------------------------------------------------------------------

NORMAL_ARTICLE = """
Researchers announced today that a new study of coral reef recovery shows
measurable improvement in several previously bleached reef systems. The
study, conducted over three years, tracked water temperature, coral cover,
and fish population density across a dozen sites in the Pacific.

"We were surprised by how quickly some of these reefs bounced back once
temperatures stabilized," said the lead author. The team plans to publish
the full dataset later this year and continue monitoring the sites through
the next bleaching season, which typically begins in late summer.

Local conservation groups welcomed the findings, noting that the results
support continued investment in marine protected areas as a practical tool
for reef resilience, even as global ocean temperatures continue to rise.
""" * 2

COOKIE_WALL = """
We use cookies to improve your experience on this site.
Manage your cookie preferences or accept cookies to continue.
By using this site you agree to our use of cookies.
"""

LOGIN_WALL = """
Please log in to continue reading this article.
Sign in to continue or create a free account to access unlimited articles.
Subscribe to continue enjoying full access to our journalism.
"""

JS_REQUIRED = """
This page requires JavaScript to run.
Please enable JavaScript in your browser and reload the page.
JavaScript is disabled in this browser. Please enable it to view this site.
"""

ERROR_404 = """
404 Not Found
The page you requested could not be found. It may have been moved or deleted.
"""

LINK_FARM = "\n".join(
    [f"Category {i}" for i in range(20)]
    + [f"Tag {i}" for i in range(20)]
)


def test_extraction_quality_normal_article_is_high_quality_no_flags():
    result = extraction_quality(NORMAL_ARTICLE)
    assert result["quality"] > 0.7
    assert result["flags"] == []


def test_extraction_quality_cookie_wall_flagged():
    result = extraction_quality(COOKIE_WALL)
    assert "paywall_or_login" in result["flags"]
    assert result["quality"] < 1.0


def test_extraction_quality_login_wall_flagged():
    result = extraction_quality(LOGIN_WALL)
    assert "paywall_or_login" in result["flags"]


def test_extraction_quality_js_required_flagged():
    result = extraction_quality(JS_REQUIRED)
    assert "js_required" in result["flags"]


def test_extraction_quality_404_flagged_error_page():
    result = extraction_quality(ERROR_404)
    assert "error_page" in result["flags"]
    assert result["quality"] < 0.3


def test_extraction_quality_link_farm_flagged():
    result = extraction_quality(LINK_FARM)
    assert "link_farm" in result["flags"] or "boilerplate_heavy" in result["flags"]


def test_extraction_quality_empty_text_is_thin():
    result = extraction_quality("")
    assert "thin" in result["flags"]
    assert result["quality"] < 1.0


def test_quality_note_present_for_error_page_and_absent_for_clean_text():
    assert quality_note(["error_page"])
    assert quality_note(["paywall_or_login"])
    assert quality_note(["js_required"])
    assert quality_note([]) == ""
    assert quality_note(["thin"]) == ""


# ---------------------------------------------------------------------------
# ranking wiring: source_type/is_official attached, nudge shows in score_reasons
# ---------------------------------------------------------------------------

def test_ranking_attaches_source_type_and_is_official():
    results = [
        {
            "title": "Python docs",
            "url": "https://docs.python.org/3/library/re.html",
            "snippet": "Regular expression operations for python programming.",
        },
    ]
    ranked = rank_search_results("python programming", results)
    assert ranked[0]["source_type"] == "docs"
    assert ranked[0]["is_official"] is True


def test_ranking_nudges_official_docs_up_with_explained_reason():
    results = [
        {
            "title": "Requests library docs",
            "url": "https://docs.python.org/3/library/requests.html",
            "snippet": "requests library documentation reference",
        },
        {
            "title": "Some blog about requests library",
            "url": "https://randomblog.example/requests-library-tips",
            "snippet": "requests library documentation reference",
        },
    ]
    ranked = rank_search_results("requests library documentation", results)
    docs_item = next(r for r in ranked if r["url"].startswith("https://docs.python.org"))
    assert any("source type" in reason for reason in docs_item["score_reasons"])
    # docs result should not rank below the otherwise-identical blog result
    assert ranked.index(docs_item) <= 1


def test_ranking_nudges_shop_down_for_non_shopping_query():
    results = [
        {
            "title": "Python tutorial for beginners",
            "url": "https://www.amazon.com/dp/B000000001",
            "snippet": "python tutorial for beginners guide",
        },
        {
            "title": "Python tutorial for beginners",
            "url": "https://realpython-like-example.example/tutorial",
            "snippet": "python tutorial for beginners guide",
        },
    ]
    ranked = rank_search_results("python tutorial for beginners", results)
    shop_item = next(r for r in ranked if "amazon.com" in r["url"])
    assert any("shop" in reason for reason in shop_item["score_reasons"])


def test_ranking_does_not_penalize_shop_when_purchase_intent_present():
    results = [
        {
            "title": "Buy running shoes",
            "url": "https://www.amazon.com/dp/B000000002",
            "snippet": "buy running shoes online, best price",
        },
    ]
    ranked = rank_search_results("buy running shoes price", results)
    assert not any("no purchase intent" in reason for reason in ranked[0]["score_reasons"])


# ---------------------------------------------------------------------------
# fetch wiring: fetch_webpage_content result carries the new fields
# (monkeypatch network via _get_public_url)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text, status_code=200, content_type="text/html; charset=utf-8"):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.truncated = False
        self.declared_bytes = len(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_fetch_webpage_content_carries_source_type_and_extraction_quality(monkeypatch):
    from services.search import content as content_mod

    html = "<html><head><title>Doc</title></head><body><article class='content'>" + \
        ("Long article content about reef recovery and marine science. " * 20) + \
        "</article></body></html>"

    monkeypatch.setattr(content_mod, "_get_public_url", lambda *a, **k: _FakeResponse(html))
    monkeypatch.setattr(content_mod, "CONTENT_CACHE_DIR", content_mod.CONTENT_CACHE_DIR)

    result = content_mod.fetch_webpage_content("https://docs.python.org/3/library/x.html")
    assert result["success"] is True
    assert result["source_type"] == "docs"
    assert result["is_official"] is True
    assert "extraction_quality" in result
    assert "quality" in result["extraction_quality"]
    assert "flags" in result["extraction_quality"]


def test_fetch_webpage_content_error_page_gets_quality_note(monkeypatch):
    from services.search import content as content_mod

    monkeypatch.setattr(
        content_mod, "_get_public_url",
        lambda *a, **k: _FakeResponse("Forbidden", status_code=403),
    )

    result = content_mod.fetch_webpage_content("https://example.com/blocked")
    assert result["success"] is False
    assert "extraction_quality" in result
    assert "error_page" in result["extraction_quality"]["flags"]
    assert result.get("quality_note")


def test_hosted_docs_domains_are_docs():
    from src.source_types import classify_source
    assert classify_source("https://python.readthedocs.io/fr/latest/x.html")["source_type"] == "docs"
    assert classify_source("https://www.boe.es/diario_boe/")["is_official"] is True
    assert classify_source("https://www.europapress.es/economia/x")["source_type"] == "news"
