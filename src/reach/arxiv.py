"""`arxiv` channel: the public arXiv API (Atom feed) -- search + abstract by
id, no auth."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client

_BASE = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")


def _extract_id(url_or_id: str) -> str:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url/id")
    m = _ID_RE.search(raw)
    if m:
        return m.group(1)
    raise ReachBackendError(f"not a recognizable arXiv id/url: {raw!r}")


def _parse_entry(entry: ET.Element) -> ReachResult:
    title = (entry.findtext(f"{_ATOM}title") or "").strip()
    summary = (entry.findtext(f"{_ATOM}summary") or "").strip()
    published = entry.findtext(f"{_ATOM}published") or ""
    id_url = entry.findtext(f"{_ATOM}id") or ""
    authors = [a.findtext(f"{_ATOM}name") or "" for a in entry.findall(f"{_ATOM}author")]
    return ReachResult(
        channel="arxiv", url=id_url, title=title, author=", ".join(a for a in authors if a),
        text=summary, published_at=published, source_trust="public_api",
    )


class ArxivApiBackend(Backend):
    name = "arxiv_api"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public arXiv API, no auth", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get(_BASE, params={"search_query": "all:test", "max_results": 1})
            return Availability(status="ready" if resp.status_code == 200 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        arxiv_id = _extract_id(url_or_id)
        async with make_client() as client:
            resp = await client.get(_BASE, params={"id_list": arxiv_id})
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ReachBackendError(f"malformed XML: {exc}") from exc
        entry = root.find(f"{_ATOM}entry")
        if entry is None:
            raise ReachBackendError(f"no entry for arXiv id {arxiv_id}")
        return _parse_entry(entry)

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        async with make_client() as client:
            resp = await client.get(_BASE, params={"search_query": f"all:{query}", "max_results": kwargs.get("limit", 10)})
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ReachBackendError(f"malformed XML: {exc}") from exc
        return [_parse_entry(e) for e in root.findall(f"{_ATOM}entry")]


class ArxivChannel(Channel):
    name = "arxiv"
    backends = [ArxivApiBackend()]
