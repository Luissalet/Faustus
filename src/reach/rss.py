"""`rss` channel: `feedparser` when installed, otherwise a minimal RSS
2.0 / Atom parser over `xml.etree` so RSS/Atom reading never hard-depends
on an optional package."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _parse_minimal(xml_bytes: bytes, url: str) -> ReachResult:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReachBackendError(f"malformed XML: {exc}") from exc

    items: list[dict[str, Any]] = []
    title = ""
    if root.tag.endswith("feed"):  # Atom
        t = root.find(f"{_ATOM_NS}title")
        title = (t.text or "").strip() if t is not None else ""
        for entry in root.findall(f"{_ATOM_NS}entry"):
            et_ = entry.find(f"{_ATOM_NS}title")
            summary = entry.find(f"{_ATOM_NS}summary")
            if summary is None:
                summary = entry.find(f"{_ATOM_NS}content")
            link_el = entry.find(f"{_ATOM_NS}link")
            date_el = entry.find(f"{_ATOM_NS}updated") or entry.find(f"{_ATOM_NS}published")
            items.append({
                "author": (entry.findtext(f"{_ATOM_NS}author/{_ATOM_NS}name") or ""),
                "text": ((et_.text if et_ is not None else "") or "") + "\n" + ((summary.text if summary is not None else "") or ""),
                "score": 0,
                "at": (date_el.text or "") if date_el is not None else "",
            })
    else:  # RSS 2.0
        channel = root.find("channel")
        base = channel if channel is not None else root
        t = base.find("title")
        title = (t.text or "").strip() if t is not None else ""
        for item in base.findall("item"):
            items.append({
                "author": item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "",
                "text": (item.findtext("title") or "") + "\n" + (item.findtext("description") or ""),
                "score": 0,
                "at": item.findtext("pubDate") or "",
            })
    if not items and not title:
        raise ReachBackendError("no title or items found -- not RSS/Atom?")
    return ReachResult(channel="rss", url=url, title=title, items=items, source_trust="public_api")


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
             "score": 0, "at": e.get("published", "")}
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
