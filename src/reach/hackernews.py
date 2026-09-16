"""`hackernews` channel: Algolia HN Search API -- search + item (with
comments), a single public backend with no auth."""
from __future__ import annotations

import re
from typing import Any

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client

_BASE = "https://hn.algolia.com/api/v1"
_ITEM_RE = re.compile(r"item\?id=(\d+)")


def _extract_item_id(url_or_id: str) -> str:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url/id")
    if raw.isdigit():
        return raw
    m = _ITEM_RE.search(raw)
    if m:
        return m.group(1)
    raise ReachBackendError(f"not a recognizable Hacker News item: {raw!r}")


def _flatten_comments(node: dict, out: list[dict]) -> None:
    for child in node.get("children") or []:
        if child.get("text"):
            out.append({
                "author": child.get("author", ""), "text": child.get("text", ""),
                "score": child.get("points") or 0, "at": child.get("created_at", ""),
            })
        _flatten_comments(child, out)


class AlgoliaHnBackend(Backend):
    name = "algolia_hn"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="Algolia HN Search API, no auth", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get(f"{_BASE}/search", params={"query": "test", "hitsPerPage": 1})
            return Availability(status="ready" if resp.status_code == 200 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        item_id = _extract_item_id(url_or_id)
        async with make_client() as client:
            resp = await client.get(f"{_BASE}/items/{item_id}")
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        comments: list[dict] = []
        _flatten_comments(data, comments)
        return ReachResult(
            channel="hackernews", url=f"https://news.ycombinator.com/item?id={item_id}",
            title=data.get("title", ""), author=data.get("author", ""), text=data.get("text") or data.get("title", ""),
            items=comments, published_at=data.get("created_at", ""), source_trust="public_api",
        )

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        async with make_client() as client:
            resp = await client.get(f"{_BASE}/search", params={"query": query, "hitsPerPage": kwargs.get("limit", 10)})
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        out = []
        for hit in data.get("hits", []):
            item_id = hit.get("objectID", "")
            out.append(ReachResult(
                channel="hackernews", url=f"https://news.ycombinator.com/item?id={item_id}",
                title=hit.get("title") or hit.get("story_title") or "", author=hit.get("author", ""),
                text=hit.get("story_text") or hit.get("comment_text") or "",
                published_at=hit.get("created_at", ""), source_trust="public_api",
            ))
        return out


class HackernewsChannel(Channel):
    name = "hackernews"
    backends = [AlgoliaHnBackend()]
