"""Live feeds with a cache that keeps answering when the source does not.

For public data that changes by the minute (earthquakes, flights, storms, wind, orbital elements, stock or price pages):
one ``LiveFeed`` per source keeps the last good answer per key in memory and on disk, lets only one request per key go out
at a time (the others wait for it), retries with growing pauses, and when the source fails serves the last good answer
marked ``stale`` with its age instead of an error.

    feed = LiveFeed("usgs", ttl_s=120, disk_dir=data_dir / "feeds")
    got = feed.get("4.5_day", lambda: fetch_json(url))
    got.data, got.freshness, got.age_s          # freshness: fresh | cached | stale

``fresh``: fetched now. ``cached``: younger than ``ttl_s``, not fetched. ``stale``: the fetch failed (``error`` says why)
and the answer is older than ``ttl_s``; ``max_stale_s`` bounds how old it may be (older -> the error is raised).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["FeedResult", "LiveFeed", "LiveFeedError"]


class LiveFeedError(RuntimeError):
    """The source failed and there is no answer young enough to serve instead."""


@dataclass
class FeedResult:
    data: Any
    freshness: str            # fresh | cached | stale
    fetched_at: float
    age_s: float
    source: str
    key: str
    error: str = ""
    attempts: int = 0

    def meta(self) -> dict[str, Any]:
        return {"freshness": self.freshness, "fetched_at": round(self.fetched_at, 3), "age_s": round(self.age_s, 1),
                "source": self.source, **({"error": self.error} if self.error else {})}


class LiveFeed:
    def __init__(self, source: str, *, ttl_s: float = 60.0, max_stale_s: Optional[float] = 7 * 24 * 3600,
                 disk_dir: Optional[Path] = None, retries: int = 2, backoff_s: float = 0.5,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.source = source
        self.ttl_s, self.max_stale_s = float(ttl_s), max_stale_s
        self.disk_dir = Path(disk_dir) / source if disk_dir else None
        self.retries, self.backoff_s = max(0, int(retries)), float(backoff_s)
        self.clock, self.sleep = clock, sleep
        self._mem: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- storage -------------------------------------------------------------------------------------------------------
    def _path(self, key: str) -> Optional[Path]:
        if not self.disk_dir:
            return None
        return self.disk_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json")

    def _load(self, key: str) -> Optional[tuple[float, Any]]:
        hit = self._mem.get(key)
        if hit is not None:
            return hit
        path = self._path(key)
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                hit = (float(raw["fetched_at"]), raw["data"])
                self._mem[key] = hit
                return hit
            except (OSError, ValueError, KeyError, TypeError):
                return None
        return None

    def _store(self, key: str, fetched_at: float, data: Any) -> None:
        self._mem[key] = (fetched_at, data)
        path = self._path(key)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "fetched_at": fetched_at, "data": data}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except (OSError, TypeError, ValueError):
            pass                                              # the memory copy still serves; a disk hiccup is not a failure

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # -- the one entry point --------------------------------------------------------------------------------------------
    def get(self, key: str, fetch: Callable[[], Any], *, ttl_s: Optional[float] = None, force: bool = False) -> FeedResult:
        ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        with self._lock(key):                                 # single flight: concurrent callers wait for one fetch
            now = self.clock()
            hit = self._load(key)
            if hit and not force and now - hit[0] < ttl:
                return FeedResult(hit[1], "cached", hit[0], now - hit[0], self.source, key)
            error, attempts = "", 0
            for attempt in range(self.retries + 1):
                attempts = attempt + 1
                try:
                    data = fetch()
                except Exception as exc:                      # any failure of the source is the same to the caller
                    error = f"{type(exc).__name__}: {exc}"[:300]
                    if attempt < self.retries:
                        self.sleep(self.backoff_s * (2 ** attempt))
                    continue
                fetched = self.clock()
                self._store(key, fetched, data)
                return FeedResult(data, "fresh", fetched, 0.0, self.source, key, attempts=attempts)
            now = self.clock()
            if hit and (self.max_stale_s is None or now - hit[0] <= self.max_stale_s):
                return FeedResult(hit[1], "stale", hit[0], now - hit[0], self.source, key, error=error, attempts=attempts)
            raise LiveFeedError(f"{self.source}: {error}" + (" (the last good answer is too old)" if hit else ""))

    def forget(self, key: str) -> None:
        self._mem.pop(key, None)
        path = self._path(key)
        if path and path.exists():
            path.unlink()
