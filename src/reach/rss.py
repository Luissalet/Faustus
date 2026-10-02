"""`rss` channel: `feedparser` when installed, otherwise a minimal RSS
2.0 / Atom parser over `xml.etree` so RSS/Atom reading never hard-depends
on an optional package."""
from __future__ import annotations

from typing import Any

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client



def _parse_minimal(xml_bytes: bytes, url: str) -> ReachResult:
    from src.hoard_link.web.feeds import parse_feed
    feed = parse_feed(xml_bytes, url)
    if feed is None:
        raise ReachBackendError("malformed XML or not RSS/Atom")
    items = [{"author": item["author"], "text": item["title"] + "\n" + item["summary"],
              "score": 0, "at": item["updated"] or item["published"]} for item in feed["items"]]
    return ReachResult(channel="rss", url=url, title=feed["title"], items=items, source_trust="public_api")


class FeedparserBackend(Backend):
    name = "feedparser"

    async def _probe(self, live: bool) -> Availability:
        try:
            import feedparser  # noqa: F401
        except ImportError:
            return Availability(status="needs_config", reason="pip install feedparser (optional; falls back to a minimal parser)", checked_live=live)
        return Availability(status="ready", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        try:
            import feedparser
        except ImportError as exc:
            raise ReachBackendError("feedparser not installed") from exc
        import asyncio

        url = url_or_id.strip()
        parsed = await asyncio.to_thread(feedparser.parse, url)
        if parsed.get("bozo") and not parsed.get("entries"):
            raise ReachBackendError(str(parsed.get("bozo_exception") or "feed parse error"))
        feed = parsed.get("feed", {})
        items = [
            {"author": e.get("author", ""), "text": f"{e.get('title', '')}\n{e.get('summary', '')}",
             "score": 0, "at": e.get("updated") or e.get("published", "")}
            for e in parsed.get("entries", [])
        ]
        if not items and not feed.get("title"):
            raise ReachBackendError("empty feed")
        return ReachResult(channel="rss", url=url, title=feed.get("title", ""), items=items, source_trust="public_api")


class MinimalXmlBackend(Backend):
    name = "minimal_xml"

    async def _probe(self, live: bool) -> Availability:
        return Availability(status="ready", reason="stdlib xml.etree, no dependency", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        url = url_or_id.strip()
        async with make_client() as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        return _parse_minimal(resp.content, url)


class RssChannel(Channel):
    name = "rss"
    backends = [FeedparserBackend(), MinimalXmlBackend()]
