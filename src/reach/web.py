"""`web` channel: built-in fetch, dedicated local browser, optional Jina mirror.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client
from src.reach import credentials
from src.settings import get_setting


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ReachBackendError("empty url")
    if "://" not in url:
        url = "https://" + url
    return url


class FaustusWebFetchBackend(Backend):
    """Reuses the existing `web_fetch` tool's implementation
    (`services.search.content.fetch_webpage_content`) instead of duplicating
    an HTML->text pipeline. Sync call -> `asyncio.to_thread`, per contract
    rule 4."""

    name = "faustus_web_fetch"

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        from services.search import comprehensive_web_search
        text, sources = await asyncio.to_thread(
            comprehensive_web_search, query, max_pages=3, return_sources=True)
        if not sources:
            raise ReachBackendError(text[:200] or "web search returned no sources")
        return [ReachResult(channel="web", url=str(source.get("url") or ""),
                            title=str(source.get("title") or ""),
                            text=str(source.get("snippet") or source.get("content") or "")[:2000],
                            source_trust="scrape") for source in sources if source.get("url")]

    async def _probe(self, live: bool) -> Availability:
        return Availability(status="ready", reason="uses Faustus's built-in fetcher", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        from services.search.content import fetch_webpage_content

        url = _normalize_url(url_or_id)
        result = await asyncio.to_thread(fetch_webpage_content, url)
        text = (result.get("content") or "").strip()
        if not text:
            raise ReachBackendError(result.get("error") or "no readable text content")
        return ReachResult(
            channel="web", url=url, title=result.get("title") or "", text=text,
            truncated=bool(result.get("truncated")), source_trust="scrape",
        )


class JinaReaderBackend(Backend):
    """`https://r.jina.ai/<url>` -- clean Markdown, no headless browser
    needed. Off entirely when `reach_jina_enabled` is false; an optional
    `reach_jina_api_key` (via `reach_<channel>_token`-style credential,
    stored under settings key `reach_jina_api_key`) raises rate limits."""

    name = "jina_reader"

    def _api_key(self) -> str:
        from src import secret_storage
        raw = get_setting("reach_jina_api_key", "") or ""
        if not raw:
            return ""
        try:
            return secret_storage.decrypt(raw)
        except Exception:
            return ""

    async def _probe(self, live: bool) -> Availability:
        if not get_setting("reach_jina_enabled", False):
            return Availability(status="unavailable", reason="reach_jina_enabled is off", checked_live=live)
        if not live:
            return Availability(status="ready", reason="Jina Reader (no auth required for basic use)", checked_live=live)
        try:
            async with make_client(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get("https://r.jina.ai/https://example.com")
            if resp.status_code < 500:
                return Availability(status="ready", reason=f"HTTP {resp.status_code}", checked_live=True)
            return Availability(status="unavailable", reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        if not get_setting("reach_jina_enabled", False):
            raise ReachBackendError("reach_jina_enabled is off")
        url = _normalize_url(url_or_id)
        headers = {}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with make_client(headers=headers) as client:
                resp = await client.get(f"https://r.jina.ai/{url}")
        except httpx.HTTPError as exc:
            raise ReachBackendError(f"network error: {exc}") from exc
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        text = resp.text.strip()
        if not text:
            raise ReachBackendError("empty response")
        title = ""
        for line in text.splitlines()[:5]:
            if line.strip().startswith("Title:"):
                title = line.split("Title:", 1)[1].strip()
                break
        return ReachResult(channel="web", url=url, title=title, text=text, source_trust="mirror")


class BrowserSessionBackend(Backend):
    """Reads through an owner-scoped local profile or an injected session reader.
    Cookies survive manual login; the normal personal browser is never reused.
    """

    name = "browser_session"

    async def _probe(self, live: bool) -> Availability:
        from src.reach.browser import browser_for
        browser = browser_for()
        return Availability(status="ready" if browser.available() else "unavailable",
                            reason=browser.unavailable_reason() or "local owner-scoped browser",
                            checked_live=False)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        ctx = kwargs.get("ctx") or {}
        reader = ctx.get("browser_session_reader") if isinstance(ctx, dict) else None
        if not callable(reader):
            from src.reach.browser import BrowserRequest, run_browser
            async def reader(target):
                return await run_browser(BrowserRequest(url=target), owner=ctx.get("owner"))
        url = _normalize_url(url_or_id)
        data = await reader(url)
        text = (data or {}).get("text") or ""
        if not text:
            raise ReachBackendError("browser session returned no text")
        return ReachResult(
            channel="web", url=url, title=(data or {}).get("title", ""), text=text,
            source_trust="browser_session",
        )


class WebChannel(Channel):
    name = "web"
    backends = [FaustusWebFetchBackend(), BrowserSessionBackend(), JinaReaderBackend()]
