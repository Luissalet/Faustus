"""`reddit` channel: (1) public `.json` endpoints (no auth), (2) `old.reddit.com`
via Jina Reader, (3) an active Faustus browser session."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client
from src.reach.web import BrowserSessionBackend, JinaReaderBackend

_REDDIT_UA = {"User-Agent": "FaustusReach/1.0 (by /u/faustus-agent)"}


def _to_json_url(url: str) -> str:
    url = url.split("?")[0].rstrip("/")
    if url.endswith(".json"):
        return url
    return url + ".json"


def _normalize_reddit_url(url_or_id: str) -> str:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url")
    if raw.startswith("r/") or raw.startswith("/r/"):
        raw = "https://www.reddit.com/" + raw.lstrip("/")
    if "reddit.com" not in raw:
        raise ReachBackendError(f"not a reddit URL: {raw!r}")
    if "://" not in raw:
        raw = "https://" + raw
    return raw


class RedditJsonBackend(Backend):
    name = "reddit_json"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public reddit .json endpoints, no auth", checked_live=live)
        try:
            async with make_client(headers=_REDDIT_UA) as client:
                resp = await client.get("https://www.reddit.com/r/test.json", params={"limit": 1})
            if resp.status_code == 200:
                return Availability(status="ready", checked_live=True)
            return Availability(status="unavailable", reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        url = _normalize_reddit_url(url_or_id)
        json_url = _to_json_url(url)
        async with make_client(headers=_REDDIT_UA) as client:
            resp = await client.get(json_url)
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ReachBackendError(f"non-JSON response: {exc}") from exc

        # Thread: list of two Listings [post, comments]. Subreddit listing:
        # a single Listing of posts.
        if isinstance(data, list) and data:
            post_listing = data[0]
            post = (post_listing.get("data", {}).get("children") or [{}])[0].get("data", {})
            comments_listing = data[1] if len(data) > 1 else {"data": {"children": []}}
            items = []
            for child in comments_listing.get("data", {}).get("children", []):
                c = child.get("data", {})
                if child.get("kind") != "t1":
                    continue
                items.append({
                    "author": c.get("author", ""), "text": c.get("body", ""),
                    "score": c.get("score", 0), "at": str(c.get("created_utc", "")),
                })
            return ReachResult(
                channel="reddit", url=url, title=post.get("title", ""), author=post.get("author", ""),
                text=post.get("selftext") or post.get("title", ""), items=items,
                published_at=str(post.get("created_utc", "")), source_trust="public_api",
            )
        # Subreddit/search listing
        posts = (data.get("data", {}) or {}).get("children", [])
        items = []
        for child in posts:
            p = child.get("data", {})
            items.append({
                "author": p.get("author", ""), "text": p.get("title", ""),
                "score": p.get("score", 0), "at": str(p.get("created_utc", "")),
            })
        return ReachResult(
            channel="reddit", url=url, title=f"{len(items)} posts", items=items,
            source_trust="public_api",
        )

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        subreddit = kwargs.get("subreddit")
        params = {"q": query, "limit": kwargs.get("limit", 10), "sort": "relevance"}
        base = f"https://www.reddit.com/r/{subreddit}/search.json" if subreddit else "https://www.reddit.com/search.json"
        if subreddit:
            params["restrict_sr"] = "1"
        async with make_client(headers=_REDDIT_UA) as client:
            resp = await client.get(base, params=params)
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        out = []
        for child in (data.get("data", {}) or {}).get("children", []):
            p = child.get("data", {})
            out.append(ReachResult(
                channel="reddit", url="https://www.reddit.com" + p.get("permalink", ""), title=p.get("title", ""),
                author=p.get("author", ""), text=p.get("selftext") or "",
                published_at=str(p.get("created_utc", "")), source_trust="public_api",
            ))
        return out


class RedditOldJinaBackend(Backend):
    name = "old_reddit_jina"

    async def _probe(self, live: bool) -> Availability:
        return await JinaReaderBackend()._probe(live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        url = _normalize_reddit_url(url_or_id)
        old_url = url.replace("://www.reddit.com", "://old.reddit.com").replace("://reddit.com", "://old.reddit.com")
        result = await JinaReaderBackend().read(old_url)
        result.channel = "reddit"
        return result


class RedditBrowserSessionBackend(BrowserSessionBackend):
    name = "browser_session"

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        result = await super().read(_normalize_reddit_url(url_or_id), **kwargs)
        result.channel = "reddit"
        return result


class RedditChannel(Channel):
    name = "reddit"
    backends = [RedditJsonBackend(), RedditOldJinaBackend(), RedditBrowserSessionBackend()]
