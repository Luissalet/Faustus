"""A page a plain fetch is refused on is read with the integrated browser.

Seen live (22-10): SteamDB and Wikipedia answered web_fetch with a
Cloudflare challenge / HTTP 403; the error told the model to "open the site
in a browser", it never did, and the answer lacked the numbers it needed.
"""
import asyncio

from src.agent_tools import web_tools as wt


class _Mgr:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name.endswith("browser_wait_for"):
            return {"stdout": "", "exit_code": 0}
        text = self.pages.pop(0) if self.pages else ""
        return {"stdout": text, "exit_code": 0}


def _blocked(monkeypatch, mgr):
    import src.search.content as content
    monkeypatch.setattr(content, "fetch_webpage_content", lambda url, **kw: {
        "error": "blocked: Cloudflare challenge page", "read_status": "blocked",
        "blocked_reason": "cloudflare", "http_status": 403})
    import src.tool_utils as tu
    monkeypatch.setattr(tu, "get_mcp_manager", lambda: mgr)


def _run(ctx, url="https://steamdb.info/app/1/charts/"):
    return asyncio.run(wt.WebFetchTool().execute(url, ctx))


def test_a_blocked_page_is_read_with_the_browser(monkeypatch):
    mgr = _Mgr(["- heading \"Minecraft Dungeons II\"\n- text: All-time peak 32,556"])
    _blocked(monkeypatch, mgr)
    out = _run({"owner": "admin", "disabled_tools": set()})
    assert out["exit_code"] == 0 and out["read_with"] == "integrated_browser"
    assert "32,556" in out["output"] and "read with the integrated browser" in out["output"]
    assert mgr.calls[0] == ("mcp__builtin_browser__browser_navigate", {"url": "https://steamdb.info/app/1/charts/"})
    assert out["evidence_refs"]


def test_a_challenge_page_is_waited_out_then_given_up(monkeypatch):
    mgr = _Mgr(["Just a moment...", "Just a moment...", "Just a moment...", "Just a moment..."])
    _blocked(monkeypatch, mgr)
    out = _run({"disabled_tools": set()})
    assert out["exit_code"] == 1 and out["read_status"] == "blocked"   # the original error stands


def test_a_challenge_that_clears_is_read(monkeypatch):
    mgr = _Mgr(["Just a moment...", "- text: Peak players 25,156"])
    _blocked(monkeypatch, mgr)
    out = _run({"disabled_tools": set()})
    assert out["exit_code"] == 0 and "25,156" in out["output"]


def test_the_browser_switched_off_keeps_the_original_error(monkeypatch):
    mgr = _Mgr(["anything"])
    _blocked(monkeypatch, mgr)
    out = _run({"disabled_tools": {"builtin_browser"}})
    assert out["exit_code"] == 1 and not mgr.calls


def test_a_browser_error_keeps_the_original_error(monkeypatch):
    class _Failing(_Mgr):
        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return {"error": "The built-in browser is switched off in Settings (disabled_tools).", "exit_code": 1}
    mgr = _Failing([])
    _blocked(monkeypatch, mgr)
    out = _run({"disabled_tools": set()})
    assert out["exit_code"] == 1 and out["read_status"] == "blocked"


def test_a_refusal_reported_as_a_network_error_is_read_with_the_browser(monkeypatch):
    # The family fetcher's refusal reaches web_fetch as "NetworkError: blocked: HTTP 403 forbidden"
    # with no read_status (seen live on Wikipedia, 22-10).
    mgr = _Mgr(["- heading \"Minecraft Dungeons II\"\n- text: released 29 September 2026"])
    import src.search.content as content
    monkeypatch.setattr(content, "fetch_webpage_content", lambda url, **kw: {
        "error": "NetworkError: blocked: HTTP 403 forbidden; not retrying for 22 more minute(s)",
        "content": "", "title": ""})
    import src.tool_utils as tu
    monkeypatch.setattr(tu, "get_mcp_manager", lambda: mgr)
    out = asyncio.run(wt.WebFetchTool().execute("https://en.wikipedia.org/wiki/X", {"disabled_tools": set()}))
    assert out["exit_code"] == 0 and "29 September 2026" in out["output"]


def test_a_compressed_page_from_the_family_fetcher_is_not_inflated_twice():
    # The family fetcher returns a decoded body with the server's Content-Encoding header;
    # rebuilding the response with it made httpx inflate it again ("incorrect header check").
    import gzip

    import httpx
    from services.search import content as sc
    body = b"<html><title>Minecraft Dungeons II Reviews</title><p>Metascore 75</p></html>"
    headers = sc._decoded_headers({"Content-Encoding": "gzip", "Content-Length": str(len(gzip.compress(body))),
                                   "Content-Type": "text/html"})
    response = httpx.Response(200, content=body, headers=headers, request=httpx.Request("GET", "https://x.test/"))
    assert response.text.endswith("</html>") and "content-encoding" not in {k.lower() for k in headers}
