"""`wikipedia` channel: the REST summary API + the MediaWiki search action,
no auth. Language-aware (`<lang>.wikipedia.org`, default "en")."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client


def _title_and_lang(url_or_id: str) -> tuple[str, str]:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty title/url")
    if "wikipedia.org" in raw:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        lang = (parsed.hostname or "en.wikipedia.org").split(".")[0]
        title = parsed.path.rsplit("/wiki/", 1)[-1]
        if not title or title == parsed.path:
            raise ReachBackendError(f"not a /wiki/<title> URL: {raw!r}")
        return title, lang
    return raw.replace(" ", "_"), "en"


class WikipediaRestBackend(Backend):
    name = "wikipedia_rest"

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public Wikipedia REST API, no auth", checked_live=live)
        try:
            async with make_client() as client:
                resp = await client.get("https://en.wikipedia.org/api/rest_v1/page/summary/Earth")
            return Availability(status="ready" if resp.status_code == 200 else "unavailable",
                                 reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        title, lang = _title_and_lang(url_or_id)
        async with make_client() as client:
            resp = await client.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}")
        if resp.status_code == 404:
            raise ReachBackendError(f"no Wikipedia page for {title!r} ({lang})")
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        return ReachResult(
            channel="wikipedia", url=(data.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
            title=data.get("title", ""), text=data.get("extract") or "", lang=lang,
            source_trust="public_api",
        )

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        lang = kwargs.get("lang", "en")
        params = {"action": "query", "list": "search", "srsearch": query, "format": "json",
                  "srlimit": kwargs.get("limit", 10)}
        async with make_client() as client:
            resp = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params=params)
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}")
        data = resp.json()
        out = []
        for hit in (data.get("query", {}) or {}).get("search", []):
            title = hit.get("title", "")
            snippet = (hit.get("snippet") or "").replace('<span class="searchmatch">', "").replace("</span>", "")
            out.append(ReachResult(
                channel="wikipedia", url=f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                title=title, text=snippet, lang=lang, source_trust="public_api",
            ))
        return out


class WikipediaChannel(Channel):
    name = "wikipedia"
    backends = [WikipediaRestBackend()]
