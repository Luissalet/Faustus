"""Per-backend behaviour against `httpx.MockTransport` (R1) — no real
network, real fallback: backend 1 fails, backend 2 answers, and the
`ReachResult.attempts` list records both."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest


def _mock_client_factory(handler):
    def _make_client(**kwargs):
        kwargs.pop("timeout", None)
        headers = kwargs.pop("headers", None) or {}
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers, **kwargs)
    return _make_client


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

def test_web_falls_back_from_faustus_fetch_to_jina(monkeypatch):
    import src.reach.web as web_mod

    def fake_fetch(url, timeout=5, **kw):
        return {"content": "", "error": "NetworkError: dns failure"}

    import services.search.content as content_mod
    monkeypatch.setattr(content_mod, "fetch_webpage_content", fake_fetch)

    def handler(request):
        assert "example.com" in str(request.url)
        return httpx.Response(200, text="Title: Example\n\nHello from Jina")

    monkeypatch.setattr(web_mod, "make_client", _mock_client_factory(handler))

    result = asyncio.run(web_mod.WebChannel().read("example.com"))
    assert result.backend == "jina_reader"
    assert result.source_trust == "mirror"
    assert "Hello from Jina" in result.text
    assert result.attempts[0]["backend"] == "faustus_web_fetch"
    assert result.attempts[0]["ok"] is False
    assert result.attempts[1] == {"backend": "jina_reader", "ok": True, "reason": ""}


def test_web_faustus_fetch_backend_succeeds_directly(monkeypatch):
    import src.reach.web as web_mod

    def fake_fetch(url, timeout=5, **kw):
        return {"content": "real page text", "title": "A Page", "truncated": False}

    import services.search.content as content_mod
    monkeypatch.setattr(content_mod, "fetch_webpage_content", fake_fetch)

    result = asyncio.run(web_mod.WebChannel().read("example.com"))
    assert result.backend == "faustus_web_fetch"
    assert result.text == "real page text"
    assert result.attempts == [{"backend": "faustus_web_fetch", "ok": True, "reason": ""}]


def test_web_browser_session_backend_needs_a_wired_reader():
    import src.reach.web as web_mod

    backend = web_mod.BrowserSessionBackend()
    with pytest.raises(web_mod.ReachBackendError):
        asyncio.run(backend.read("https://example.com"))


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------

def test_github_api_reads_repo_with_readme(monkeypatch):
    import src.reach.github as gh_mod

    def handler(request):
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, text="# Opencode\nDetails")
        return httpx.Response(200, json={
            "full_name": "anomalyco/opencode", "html_url": "https://github.com/anomalyco/opencode",
            "description": "An agent", "owner": {"login": "anomalyco"}, "created_at": "2020-01-01",
        })

    monkeypatch.setattr(gh_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(gh_mod.GithubChannel().read("github.com/anomalyco/opencode"))
    assert result.backend == "github_api"
    assert result.title == "anomalyco/opencode"
    assert "Opencode" in result.text
    assert result.source_trust == "public_api"


def test_github_falls_back_to_jina_when_api_and_gh_cli_fail(monkeypatch):
    import src.reach.github as gh_mod

    def handler(request):
        return httpx.Response(403, json={"message": "rate limited"})

    monkeypatch.setattr(gh_mod, "make_client", _mock_client_factory(handler))
    monkeypatch.setattr(gh_mod.shutil, "which", lambda name: None)

    def jina_handler(request):
        return httpx.Response(200, text="Title: opencode\n\nMirror content")

    import src.reach.web as web_mod
    monkeypatch.setattr(web_mod, "make_client", _mock_client_factory(jina_handler))

    result = asyncio.run(gh_mod.GithubChannel().read("github.com/anomalyco/opencode"))
    assert result.backend == "jina_reader"
    assert [a["backend"] for a in result.attempts] == ["github_api", "gh_cli", "jina_reader"]
    assert result.attempts[0]["ok"] is False
    assert result.attempts[1]["ok"] is False


def test_github_token_is_sent_but_never_returned(monkeypatch):
    import src.reach.github as gh_mod
    from src.settings import DEFAULT_SETTINGS
    from src import settings as settings_mod

    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"full_name": "a/b", "html_url": "u", "owner": {"login": "a"}})

    monkeypatch.setattr(gh_mod, "make_client", _mock_client_factory(handler))
    monkeypatch.setattr(gh_mod.credentials, "get_token", lambda ch: "secret-token-xyz" if ch == "github" else "")

    result = asyncio.run(gh_mod.GithubChannel().read("github.com/a/b"))
    assert captured["auth"] == "Bearer secret-token-xyz"
    dumped = json.dumps(result.to_dict())
    assert "secret-token-xyz" not in dumped


# ---------------------------------------------------------------------------
# reddit
# ---------------------------------------------------------------------------

def test_reddit_json_reads_thread_with_comments(monkeypatch):
    import src.reach.reddit as reddit_mod

    def handler(request):
        return httpx.Response(200, json=[
            {"data": {"children": [{"data": {"title": "Post title", "author": "op", "selftext": "body text",
                                              "created_utc": 100}}]}},
            {"data": {"children": [
                {"kind": "t1", "data": {"author": "commenter", "body": "nice", "score": 5, "created_utc": 101}},
            ]}},
        ])

    monkeypatch.setattr(reddit_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(reddit_mod.RedditChannel().read("https://www.reddit.com/r/test/comments/abc/post/"))
    assert result.backend == "reddit_json"
    assert result.title == "Post title"
    assert result.items == [{"author": "commenter", "text": "nice", "score": 5, "at": "101"}]


def test_reddit_falls_back_to_old_reddit_jina(monkeypatch):
    import src.reach.reddit as reddit_mod
    import src.reach.web as web_mod

    def failing_handler(request):
        return httpx.Response(429)

    monkeypatch.setattr(reddit_mod, "make_client", _mock_client_factory(failing_handler))

    def jina_handler(request):
        assert "old.reddit.com" in str(request.url)
        return httpx.Response(200, text="Title: old reddit mirror\n\ncontent")

    monkeypatch.setattr(web_mod, "make_client", _mock_client_factory(jina_handler))

    result = asyncio.run(reddit_mod.RedditChannel().read("https://www.reddit.com/r/test/comments/abc/post/"))
    assert result.backend == "old_reddit_jina"
    assert result.channel == "reddit"


# ---------------------------------------------------------------------------
# x / twitter
# ---------------------------------------------------------------------------

def test_x_fxtwitter_then_vxtwitter_fallback(monkeypatch):
    import src.reach.x as x_mod

    def fx_fail(request):
        return httpx.Response(404, json={"message": "not found"})

    def vx_ok(request):
        return httpx.Response(200, json={
            "text": "hello world", "user_screen_name": "someone", "tweetURL": "https://x.com/someone/status/1",
            "date": "now", "mediaURLs": [],
        })

    calls = {"n": 0}

    def dispatch(request):
        calls["n"] += 1
        if "fxtwitter" in str(request.url):
            return fx_fail(request)
        return vx_ok(request)

    monkeypatch.setattr(x_mod, "make_client", _mock_client_factory(dispatch))
    result = asyncio.run(x_mod.XChannel().read("https://x.com/someone/status/1"))
    assert result.backend == "vxtwitter"
    assert result.text == "hello world"
    assert result.attempts[0]["backend"] == "fxtwitter"
    assert result.attempts[0]["ok"] is False


def test_x_search_unavailable_without_browser_session_or_nitter(monkeypatch):
    import src.reach.x as x_mod

    def dispatch(request):
        return httpx.Response(404)

    monkeypatch.setattr(x_mod, "make_client", _mock_client_factory(dispatch))
    monkeypatch.setattr(x_mod, "get_setting", lambda key, default=None: "" if key == "reach_nitter_base" else default)

    results = asyncio.run(x_mod.XChannel().search("faustus"))
    assert len(results) == 1
    assert results[0].error
    last_attempt = results[0].attempts[-1]
    assert last_attempt["backend"] == "browser_session"
    assert "neither is set" in last_attempt["reason"]


def test_x_search_uses_nitter_when_configured(monkeypatch):
    import src.reach.x as x_mod

    def dispatch(request):
        if "fxtwitter" in str(request.url) or "vxtwitter" in str(request.url) or "syndication" in str(request.url):
            return httpx.Response(404)
        if "nitter.example" in str(request.url):
            return httpx.Response(200, text="<html>nitter results</html>")
        return httpx.Response(404)

    monkeypatch.setattr(x_mod, "make_client", _mock_client_factory(dispatch))
    monkeypatch.setattr(x_mod, "get_setting", lambda key, default=None: "https://nitter.example" if key == "reach_nitter_base" else default)

    results = asyncio.run(x_mod.XChannel().search("faustus"))
    assert results[0].backend == "browser_session"
    assert "nitter results" in results[0].text


# ---------------------------------------------------------------------------
# hackernews
# ---------------------------------------------------------------------------

def test_hackernews_item_with_flattened_comments(monkeypatch):
    import src.reach.hackernews as hn_mod

    def handler(request):
        return httpx.Response(200, json={
            "title": "Some story", "author": "op", "text": "", "created_at": "t",
            "children": [{"author": "c1", "text": "first comment", "points": 3, "created_at": "t2", "children": []}],
        })

    monkeypatch.setattr(hn_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(hn_mod.HackernewsChannel().read("https://news.ycombinator.com/item?id=123"))
    assert result.backend == "algolia_hn"
    assert result.items == [{"author": "c1", "text": "first comment", "score": 3, "at": "t2"}]


# ---------------------------------------------------------------------------
# arxiv
# ---------------------------------------------------------------------------

_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>A Great Paper</title>
    <summary>An abstract.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""


def test_arxiv_reads_abstract(monkeypatch):
    import src.reach.arxiv as arxiv_mod

    def handler(request):
        return httpx.Response(200, text=_ARXIV_ATOM, headers={"content-type": "application/atom+xml"})

    monkeypatch.setattr(arxiv_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(arxiv_mod.ArxivChannel().read("2301.00001"))
    assert result.title == "A Great Paper"
    assert result.author == "Jane Doe"
    assert result.text == "An abstract."


# ---------------------------------------------------------------------------
# wikipedia
# ---------------------------------------------------------------------------

def test_wikipedia_summary(monkeypatch):
    import src.reach.wikipedia as wiki_mod

    def handler(request):
        return httpx.Response(200, json={
            "title": "Transformer", "extract": "A neural network architecture.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Transformer"}},
        })

    monkeypatch.setattr(wiki_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(wiki_mod.WikipediaChannel().read("Transformer"))
    assert result.backend == "wikipedia_rest"
    assert result.text == "A neural network architecture."


def test_wikipedia_404_raises_and_channel_reports_no_backend(monkeypatch):
    import src.reach.wikipedia as wiki_mod

    def handler(request):
        return httpx.Response(404)

    monkeypatch.setattr(wiki_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(wiki_mod.WikipediaChannel().read("Nonexistent Page Xyz"))
    assert result.backend == ""
    assert result.error


# ---------------------------------------------------------------------------
# rss
# ---------------------------------------------------------------------------

_RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>My Feed</title>
<item><title>Item One</title><description>Desc one</description><pubDate>Mon</pubDate></item>
</channel></rss>"""


def test_rss_minimal_xml_parser_when_feedparser_missing(monkeypatch):
    import src.reach.rss as rss_mod
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "feedparser":
            raise ImportError("no feedparser")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    def handler(request):
        return httpx.Response(200, content=_RSS_XML.encode())

    monkeypatch.setattr(rss_mod, "make_client", _mock_client_factory(handler))
    result = asyncio.run(rss_mod.RssChannel().read("https://example.com/feed.xml"))
    assert result.backend == "minimal_xml"
    assert result.title == "My Feed"
    assert result.items[0]["text"].startswith("Item One")


# ---------------------------------------------------------------------------
# youtube
# ---------------------------------------------------------------------------

def test_youtube_uses_faustus_handler_for_transcript_and_comments(monkeypatch):
    import src.reach.youtube as yt_mod
    import services.youtube.youtube_handler as yh

    async def fake_extract_transcript_async(url, video_id, max_retries=3):
        return {"success": True, "transcript": "hello transcript", "language": "en"}

    async def fake_fetch_comments(video_id, max_comments=25, timeout=30):
        return {"success": True, "comments": [{"author": "u1", "text": "great video", "likes": 10}],
                "title": "A Video", "channel": "A Channel"}

    monkeypatch.setattr(yh, "extract_transcript_async", fake_extract_transcript_async)
    monkeypatch.setattr(yh, "fetch_youtube_comments", fake_fetch_comments)
    monkeypatch.setattr(yh, "init_youtube", lambda: None)

    result = asyncio.run(yt_mod.YoutubeChannel().read("https://www.youtube.com/watch?v=abc12345678"))
    assert result.backend == "faustus_youtube_handler"
    assert result.text == "hello transcript"
    assert result.items == [{"author": "u1", "text": "great video", "score": 10, "at": ""}]
    assert result.title == "A Video"


def test_youtube_falls_back_to_jina_when_handler_and_transcript_api_fail(monkeypatch):
    import src.reach.youtube as yt_mod
    import services.youtube.youtube_handler as yh

    async def failing_transcript(url, video_id, max_retries=3):
        return {"success": False, "error": "blocked", "transcript": None}

    async def failing_comments(video_id, max_comments=25, timeout=30):
        return {"success": False, "comments": []}

    monkeypatch.setattr(yh, "extract_transcript_async", failing_transcript)
    monkeypatch.setattr(yh, "fetch_youtube_comments", failing_comments)
    monkeypatch.setattr(yh, "init_youtube", lambda: None)

    async def failing_transcript_api_read(self, url_or_id, **kwargs):
        raise yt_mod.ReachBackendError("youtube-transcript-api not installed")

    monkeypatch.setattr(yt_mod.YoutubeTranscriptApiBackend, "read", failing_transcript_api_read)

    def jina_handler(request):
        return httpx.Response(200, text="Title: A Video\n\npage text")

    import src.reach.web as web_mod
    monkeypatch.setattr(web_mod, "make_client", _mock_client_factory(jina_handler))

    result = asyncio.run(yt_mod.YoutubeChannel().read("https://www.youtube.com/watch?v=abc12345678"))
    assert result.backend == "jina_reader"
    assert result.channel == "youtube"
