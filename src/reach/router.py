"""Channel detection + fan-out read/search across `src/reach/*`."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from src.reach.base import ReachResult
from src.reach.arxiv import ArxivChannel
from src.reach.github import GithubChannel
from src.reach.hackernews import HackernewsChannel
from src.reach.reddit import RedditChannel
from src.reach.rss import RssChannel
from src.reach.web import WebChannel
from src.reach.wikipedia import WikipediaChannel
from src.reach.x import XChannel
from src.reach.youtube import YoutubeChannel

CHANNELS: dict[str, Any] = {
    "web": WebChannel(),
    "youtube": YoutubeChannel(),
    "github": GithubChannel(),
    "reddit": RedditChannel(),
    "x": XChannel(),
    "hackernews": HackernewsChannel(),
    "rss": RssChannel(),
    "arxiv": ArxivChannel(),
    "wikipedia": WikipediaChannel(),
}

_FEED_PATH_HINTS = ("/feed", "/rss", "/atom")


def detect_channel(url_or_query: str) -> str:
    """Best-effort channel detection by domain/pattern. Falls back to
    `"web"` for anything that is not a recognizable URL of a specific
    channel, or a bare search-style query."""
    raw = (url_or_query or "").strip()
    if not raw:
        return "web"
    low = raw.lower()
    if "://" not in low and "." not in low.split("/")[0]:
        # No scheme and the first path segment has no dot -> not a URL at
        # all (a search query, a bare video id, etc.) -- default to web.
        return "web"
    parsed = urlparse(low if "://" in low else "https://" + low)
    host = (parsed.hostname or "").lstrip("www.")
    path = parsed.path or ""

    if host in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"):
        return "youtube"
    if host in ("x.com", "twitter.com", "mobile.twitter.com"):
        return "x"
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    if host == "github.com":
        return "github"
    if host == "news.ycombinator.com":
        return "hackernews"
    if host == "arxiv.org":
        return "arxiv"
    if host.endswith("wikipedia.org"):
        return "wikipedia"
    if path.endswith((".xml", ".rss")) or any(hint in path for hint in _FEED_PATH_HINTS):
        return "rss"
    return "web"


async def read(url: str, **kwargs: Any) -> ReachResult:
    channel_name = kwargs.pop("channel", None) or detect_channel(url)
    channel = CHANNELS.get(channel_name, CHANNELS["web"])
    return await channel.read(url, **kwargs)


async def search(query: str, channels: Optional[list[str]] = None, limit: int = 10, **kwargs: Any) -> dict[str, list[ReachResult]]:
    """Search across one or more channels; returns `{channel_name: [ReachResult, ...]}`.

    Without an explicit `channels` list, defaults to `["web"]` -- fanning a
    query out across every channel by default would be surprising (and slow)
    for what is usually a plain web search.
    """
    targets = channels or ["web"]
    out: dict[str, list[ReachResult]] = {}
    for name in targets:
        channel = CHANNELS.get(name)
        if channel is None:
            out[name] = [ReachResult(channel=name, error=f"unknown channel: {name!r}")]
            continue
        out[name] = await channel.search(query, limit=limit, **kwargs)
    return out
