"""`x` (Twitter) channel: (1) fxtwitter/vxtwitter public JSON mirrors,
(2) the syndication widget endpoint, (3) Jina Reader, (4) an active browser
session. Search on X has NO public API and no reliable mirror: it only
works through a browser session, or a user-configured Nitter instance
(`reach_nitter_base`) -- with neither, `search` reports `unavailable` with
that reason instead of inventing results.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client
from src.reach.web import BrowserSessionBackend, JinaReaderBackend
from src.settings import get_setting

_STATUS_RE = re.compile(r"(?:x\.com|twitter\.com)/(?P<user>[\w]+)/status/(?P<id>\d+)")


def _parse_tweet(url_or_id: str) -> dict[str, str]:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url/id")
    m = _STATUS_RE.search(raw)
    if m:
        return {"user": m["user"], "id": m["id"]}
    if raw.isdigit():
        return {"user": "i", "id": raw}
    raise ReachBackendError(f"not a recognizable X/Twitter status URL: {raw!r}")


class FxTwitterBackend(Backend):
    name = "fxtwitter"
    _base = "https://api.fxtwitter.com"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public fxtwitter.com mirror, no auth", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get(f"{self._base}/status/1")
            return Availability(status="ready" if resp.status_code < 500 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        tweet = _parse_tweet(url_or_id)
        async with make_client() as client:
            resp = await client.get(f"{self._base}/{tweet['user']}/status/{tweet['id']}")
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        t = data.get("tweet") or {}
        if not t:
            raise ReachBackendError(data.get("message") or "empty response")
        author = (t.get("author") or {})
        media = [m.get("url") for m in (t.get("media", {}).get("all") or []) if m.get("url")]
        return ReachResult(
            channel="x", url=t.get("url", url_or_id), title=(t.get("text") or "")[:120],
            author=author.get("screen_name", ""), text=t.get("text") or "", media=media,
            published_at=t.get("created_at", ""), source_trust="mirror",
        )


class VxTwitterBackend(Backend):
    name = "vxtwitter"
    _base = "https://api.vxtwitter.com"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public vxtwitter.com mirror, no auth", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get(f"{self._base}/i/status/1")
            return Availability(status="ready" if resp.status_code < 500 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        tweet = _parse_tweet(url_or_id)
        async with make_client() as client:
            resp = await client.get(f"{self._base}/{tweet['user']}/status/{tweet['id']}")
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            raise ReachBackendError(str(data.get("error")))
        media = [m.get("url") for m in (data.get("mediaURLs") or []) if isinstance(m, dict)] or (data.get("mediaURLs") or [])
        return ReachResult(
            channel="x", url=data.get("tweetURL", url_or_id), title=(data.get("text") or "")[:120],
            author=data.get("user_screen_name", ""), text=data.get("text") or "",
            media=[m for m in media if isinstance(m, str)],
            published_at=data.get("date", ""), source_trust="mirror",
        )


class SyndicationBackend(Backend):
    name = "syndication"
    _base = "https://cdn.syndication.twimg.com/tweet-result"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="twitter syndication widget endpoint", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get(self._base, params={"id": "20", "token": "x"})
            return Availability(status="ready" if resp.status_code < 500 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        tweet = _parse_tweet(url_or_id)
        async with make_client() as client:
            resp = await client.get(self._base, params={"id": tweet["id"], "token": "x"})
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        text = data.get("text") or ""
        if not text:
            raise ReachBackendError("empty response")
        user = data.get("user") or {}
        return ReachResult(
            channel="x", url=f"https://x.com/{user.get('screen_name', tweet['user'])}/status/{tweet['id']}",
            title=text[:120], author=user.get("screen_name", ""), text=text,
            published_at=data.get("created_at", ""), source_trust="mirror",
        )


class XJinaBackend(Backend):
    name = "jina_reader"

    async def _probe(self, live: bool) -> Availability:
        return await JinaReaderBackend()._probe(live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        tweet = _parse_tweet(url_or_id)
        url = f"https://x.com/{tweet['user']}/status/{tweet['id']}"
        result = await JinaReaderBackend().read(url)
        result.channel = "x"
        return result


class XBrowserSessionBackend(BrowserSessionBackend):
    name = "browser_session"

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        tweet = _parse_tweet(url_or_id)
        url = f"https://x.com/{tweet['user']}/status/{tweet['id']}"
        result = await super().read(url, **kwargs)
        result.channel = "x"
        return result

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        ctx = kwargs.get("ctx") or {}
        reader = ctx.get("browser_session_reader") if isinstance(ctx, dict) else None
        nitter_base = (get_setting("reach_nitter_base", "") or "").strip()
        if not callable(reader) and not nitter_base:
            raise ReachBackendError(
                "X search has no public API and no reliable mirror; requires an active "
                "browser session or a configured reach_nitter_base -- neither is set"
            )
        if nitter_base:
            async with make_client() as client:
                resp = await client.get(nitter_base.rstrip("/") + "/search", params={"f": "tweets", "q": query})
            if resp.status_code >= 400:
                raise ReachBackendError(f"HTTP {resp.status_code}")
            return [ReachResult(channel="x", url=nitter_base, title=f"Nitter search results for {query!r}",
                                 text=resp.text[:4000], source_trust="mirror")]
        url = f"https://x.com/search?q={query}&src=typed_query&f=live"
        data = await reader(url)
        text = (data or {}).get("text") or ""
        if not text:
            raise ReachBackendError("browser session returned no text")
        return [ReachResult(channel="x", url=url, title=f"X search results for {query!r}", text=text,
                             source_trust="browser_session")]


class XChannel(Channel):
    name = "x"
    backends = [FxTwitterBackend(), VxTwitterBackend(), SyndicationBackend(), XJinaBackend(), XBrowserSessionBackend()]
