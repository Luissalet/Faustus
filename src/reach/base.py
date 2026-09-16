"""Reach: shared shapes for "eyes on the internet" channels (R1).

A `Channel` (web, youtube, github, reddit, x, hackernews, rss, arxiv,
wikipedia) owns an ordered list of `Backend`s. `Channel.read`/`Channel.search`
try each backend IN REAL ORDER -- they make the actual call, not just a
`which`/import check -- and fall back to the next one on failure, recording
every attempt (backend name, ok/fail, reason) on the returned `ReachResult`
so the caller can see exactly which backend served the answer and why the
others did not.

`Backend.available()` is the cheap side used by `doctor.py`: with
`live=False` it only inspects local config (settings/credentials/optional
deps) with no network I/O; with `live=True` it makes ONE real, cheap request
(cached 10 minutes per `(channel, backend, live)` key) so the doctor can tell
"configured" from "actually reachable right now".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

AVAILABILITY_TTL_SECONDS = 600  # 10 minutes

_AVAILABILITY_CACHE: dict[str, tuple[float, "Availability"]] = {}


class ReachBackendError(Exception):
    """A single backend failed to serve a read/search -- the channel moves
    on to the next backend and records `str(exc)` as the failure reason."""


@dataclass
class Availability:
    """Doctor-facing status for one backend."""

    status: str  # "ready" | "needs_config" | "unavailable"
    reason: str = ""
    checked_live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "checked_live": self.checked_live}


@dataclass
class ReachResult:
    """Unified result shape every channel/backend returns."""

    channel: str
    backend: str = ""
    url: str = ""
    title: str = ""
    author: str = ""
    published_at: str = ""
    text: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    lang: str = ""
    fetched_at: str = ""
    truncated: bool = False
    source_trust: str = "scrape"  # public_api | mirror | scrape | browser_session
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "backend": self.backend,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "published_at": self.published_at,
            "text": self.text,
            "items": self.items,
            "media": self.media,
            "lang": self.lang,
            "fetched_at": self.fetched_at,
            "truncated": self.truncated,
            "source_trust": self.source_trust,
            "attempts": self.attempts,
            "error": self.error,
        }


def _now_iso() -> str:
    from src.contracts.base import now_iso
    return now_iso()


class Backend:
    """One candidate implementation for a channel (an API, a CLI, a reader
    service, a browser session...). Subclasses override `read`/`search` and,
    when it is worth telling `doctor` apart from "ready", `_probe`."""

    name: str = "backend"

    async def available(self, live: bool = False) -> Availability:
        cache_key = f"{self.__class__.__module__}.{self.__class__.__qualname__}:{live}"
        cached = _AVAILABILITY_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and (now - cached[0]) < AVAILABILITY_TTL_SECONDS:
            return cached[1]
        try:
            result = await self._probe(live=live)
        except Exception as exc:  # noqa: BLE001 - doctor must never 500
            result = Availability(status="unavailable", reason=f"probe failed: {exc}", checked_live=live)
        _AVAILABILITY_CACHE[cache_key] = (now, result)
        return result

    async def _probe(self, live: bool) -> Availability:
        """Default: assume ready with no config requirement. Override to
        check settings/optional deps (live=False) or make one cheap real
        request (live=True)."""
        return Availability(status="ready", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        raise NotImplementedError(f"{self.name}: read not supported")

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        raise NotImplementedError(f"{self.name}: search not supported")


def clear_availability_cache() -> None:
    """Test hook -- drop the 10-minute doctor cache between cases."""
    _AVAILABILITY_CACHE.clear()


class Channel:
    """Owns an ordered `backends` list and tries each one for real."""

    name: str = "channel"
    backends: list[Backend] = []

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        attempts: list[dict[str, Any]] = []
        last_error: Optional[str] = None
        for backend in self.backends:
            try:
                result = await backend.read(url_or_id, **kwargs)
            except NotImplementedError as exc:
                attempts.append({"backend": backend.name, "ok": False, "reason": str(exc) or "not supported"})
                continue
            except Exception as exc:  # noqa: BLE001
                reason = str(exc) or type(exc).__name__
                attempts.append({"backend": backend.name, "ok": False, "reason": reason})
                last_error = reason
                logger.info("reach.%s: backend %s failed: %s", self.name, backend.name, reason)
                continue
            attempts.append({"backend": backend.name, "ok": True, "reason": ""})
            result.channel = self.name
            result.backend = backend.name
            result.attempts = attempts
            if not result.fetched_at:
                result.fetched_at = _now_iso()
            return result
        return ReachResult(
            channel=self.name,
            backend="",
            url=url_or_id,
            attempts=attempts,
            error=last_error or "no backend for this channel produced a result",
            fetched_at=_now_iso(),
        )

    async def search(self, query: str, limit: int = 10, **kwargs: Any) -> list[ReachResult]:
        attempts: list[dict[str, Any]] = []
        for backend in self.backends:
            try:
                results = await backend.search(query, **kwargs)
            except NotImplementedError as exc:
                attempts.append({"backend": backend.name, "ok": False, "reason": str(exc) or "not supported"})
                continue
            except Exception as exc:  # noqa: BLE001
                reason = str(exc) or type(exc).__name__
                attempts.append({"backend": backend.name, "ok": False, "reason": reason})
                logger.info("reach.%s: backend %s search failed: %s", self.name, backend.name, reason)
                continue
            attempts.append({"backend": backend.name, "ok": True, "reason": ""})
            out = []
            for r in results[:limit]:
                r.channel = self.name
                r.backend = backend.name
                r.attempts = attempts
                if not r.fetched_at:
                    r.fetched_at = _now_iso()
                out.append(r)
            return out
        empty = ReachResult(
            channel=self.name, backend="", attempts=attempts,
            error="no backend for this channel produced results", fetched_at=_now_iso(),
        )
        return [empty]
