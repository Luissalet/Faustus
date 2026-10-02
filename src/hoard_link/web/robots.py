"""robots.txt: a rules parser with wildcard support and a cache with an injectable fetcher and clock.

``urllib.robotparser`` matches path prefixes only; real robots files use ``*`` and ``$`` (``Disallow: /*.pdf$``) and
the longest matching rule wins. :class:`RobotsRules` implements the standard behaviour (RFC 9309):

* a group is one or more ``User-agent`` lines followed by rules; the most specific group whose name appears in the
  crawler's name is used, else ``*``; groups of the same name are merged;
* ``Allow`` / ``Disallow`` values are path prefixes with ``*`` (any run of characters) and a trailing ``$`` (end of
  URL); the **longest** matching pattern wins and ``Allow`` wins a tie; an empty ``Disallow`` allows everything;
* ``/robots.txt`` itself is always allowed; ``Crawl-delay`` and ``Sitemap`` are read.

:class:`RobotsCache` keeps one parsed file per origin for ``ttl_s`` (24 h). Conventions of a polite personal crawler:
``200`` is parsed and stored; any other ``4xx`` means "no robots" (everything allowed, remembered); ``429``, ``5xx``
and network errors allow this time with a note and are retried after ``unreachable_retry_s``. The store is any
``MutableMapping[str, dict]`` (a plain ``dict`` by default; pass a persistent one to survive restarts).

The Node twin is ``robotsAllowed`` in ``js/hoard-commons/web.js`` (vectors: ``tests/vectors/web_robots.json``).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, MutableMapping, Optional
from urllib.parse import unquote, urlsplit

__all__ = ["RobotsRules", "RobotsCache", "RobotsFetcher", "DEFAULT_AGENT", "ROBOTS_TTL_S"]

ROBOTS_TTL_S = 24 * 3600
UNREACHABLE_RETRY_S = 10 * 60
DEFAULT_AGENT = "HoardLink"

# fetch_text(url) -> (status, text, error). ``status`` 0 with an ``error`` means unreachable.
RobotsFetcher = Callable[[str], tuple[int, str, str]]


def _pattern_regex(pattern: str) -> "re.Pattern[str]":
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""), re.S)


class RobotsRules:
    """A parsed robots.txt."""

    def __init__(self, text: str = ""):
        # (agent tokens, [(allow, pattern, regex)], crawl_delay)
        self.groups: list[tuple[list[str], list[tuple[bool, str, "re.Pattern[str]"]], Optional[float]]] = []
        self.sitemaps: list[str] = []
        agents: list[str] = []
        rules: list[tuple[bool, str, "re.Pattern[str]"]] = []
        delay: Optional[float] = None
        in_rules = False

        def close() -> None:
            nonlocal agents, rules, delay, in_rules
            if agents:
                self.groups.append((agents, rules, delay))
            agents, rules, delay, in_rules = [], [], None, False

        for raw in str(text or "").lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if in_rules:
                    close()
                if value:
                    agents.append(value.lower())
            elif field in ("allow", "disallow"):
                if not agents:
                    continue
                in_rules = True
                if value:
                    rules.append((field == "allow", value, _pattern_regex(value)))
            elif field == "crawl-delay":
                if agents:
                    in_rules = True
                    try:
                        delay = float(value)
                    except ValueError:
                        pass
            elif field == "sitemap" and value:
                self.sitemaps.append(value)
        close()

    def _select(self, agent: str) -> tuple[list[tuple[bool, str, "re.Pattern[str]"]], Optional[float]]:
        name = agent.lower()
        best_len = -1
        chosen: list[tuple[list[str], list, Optional[float]]] = []
        for group in self.groups:
            for token in group[0]:
                if token != "*" and token in name and len(token) >= best_len:
                    if len(token) > best_len:
                        chosen, best_len = [], len(token)
                    chosen.append(group)
                    break
        if not chosen:
            chosen = [g for g in self.groups if "*" in g[0]]
        rules = [r for g in chosen for r in g[1]]
        delays = [g[2] for g in chosen if g[2] is not None]
        return rules, (delays[0] if delays else None)

    def allowed(self, agent: str, path: str) -> bool:
        """May ``agent`` fetch ``path`` (path and query, e.g. ``/a/b?x=1``)?"""
        path = path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if path.split("?", 1)[0] == "/robots.txt":
            return True
        rules, _ = self._select(agent)
        best: Optional[tuple[int, bool]] = None
        candidates = {path, unquote(path)}
        for allow, pattern, rx in rules:
            if any(rx.match(c) for c in candidates):
                score = (len(pattern), allow)
                if best is None or score > best:
                    best = score
        return True if best is None else best[1]

    def crawl_delay(self, agent: str) -> Optional[float]:
        return self._select(agent)[1]


class RobotsCache:
    """``check(url) -> (allowed, note)`` with one robots.txt fetched per origin per ``ttl_s``."""

    def __init__(self, fetch_text: RobotsFetcher, *, store: Optional[MutableMapping[str, Any]] = None,
                 clock: Callable[[], float] = time.time, ttl_s: float = ROBOTS_TTL_S,
                 unreachable_retry_s: float = UNREACHABLE_RETRY_S, agent: str = DEFAULT_AGENT):
        self.fetch_text = fetch_text
        self.store: MutableMapping[str, Any] = {} if store is None else store
        self.clock = clock
        self.ttl_s = ttl_s
        self.unreachable_retry_s = unreachable_retry_s
        self.agent = agent
        self._parsed: dict[str, tuple[float, Optional[RobotsRules]]] = {}
        self._unreachable_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _origin(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return "", ""
        scheme = (parts.scheme or "https").lower()
        netloc = f"[{host}]" if ":" in host else host
        if parts.port and parts.port not in (80, 443):
            netloc += f":{parts.port}"
        return scheme, netloc

    def check(self, url: str) -> tuple[bool, str]:
        """``(allowed, note)``. ``note`` is non-empty when robots.txt could not be read (the fetch is allowed)."""
        scheme, netloc = self._origin(url)
        if not netloc:
            return True, ""
        rules, note = self._rules_for(scheme, netloc)
        if rules is None:
            return True, note
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        return rules.allowed(self.agent, path), ""

    def crawl_delay(self, url: str) -> Optional[float]:
        """The ``Crawl-delay`` (seconds) asked of this agent for the site, when the file is cached."""
        scheme, netloc = self._origin(url)
        if not netloc:
            return None
        rules, _ = self._rules_for(scheme, netloc)
        return rules.crawl_delay(self.agent) if rules is not None else None

    def raw(self, url_or_origin: str) -> Optional[str]:
        """The cached robots.txt text (``None`` when never fetched)."""
        scheme, netloc = self._origin(url_or_origin if "://" in url_or_origin else "https://" + url_or_origin)
        entry = self.store.get(f"{scheme}://{netloc}")
        return entry.get("text") if isinstance(entry, dict) else None

    def forget(self, url_or_origin: str) -> None:
        scheme, netloc = self._origin(url_or_origin if "://" in url_or_origin else "https://" + url_or_origin)
        key = f"{scheme}://{netloc}"
        with self._lock:
            self._parsed.pop(key, None)
            self._unreachable_until.pop(key, None)
        try:
            del self.store[key]
        except KeyError:
            pass

    # ---- internals
    def _rules_for(self, scheme: str, netloc: str) -> tuple[Optional[RobotsRules], str]:
        key = f"{scheme}://{netloc}"
        now = self.clock()
        with self._lock:
            cached = self._parsed.get(key)
            if cached and now - cached[0] < self.ttl_s:
                return cached[1], ""
            if self._unreachable_until.get(key, 0) > now:
                return None, "robots.txt unreachable (assumed allowed)"
        entry = self.store.get(key)
        if isinstance(entry, dict) and entry.get("ts") is not None and now - float(entry["ts"]) < self.ttl_s:
            rules = RobotsRules(entry.get("text", "")) if entry.get("text", "").strip() else None
            with self._lock:
                self._parsed[key] = (float(entry["ts"]), rules)
            return rules, ""
        return self._refresh(key, now)

    def _refresh(self, key: str, now: float) -> tuple[Optional[RobotsRules], str]:
        status, text, error = self.fetch_text(f"{key}/robots.txt")
        if status == 200 and text is not None:
            return self._remember(key, text, now), ""
        if 400 <= status < 500 and status != 429:           # no robots.txt (or not for us): everything is allowed
            self._remember(key, "", now)
            return None, ""
        with self._lock:                                      # 429, 5xx, network error: allow now, look again later
            self._unreachable_until[key] = now + self.unreachable_retry_s
        return None, f"robots.txt unreachable ({error or 'HTTP ' + str(status)}); assumed allowed"

    def _remember(self, key: str, text: str, now: float) -> Optional[RobotsRules]:
        try:
            self.store[key] = {"text": text, "ts": now}
        except Exception:                                      # noqa: BLE001 - a broken store must not break fetching
            pass
        rules = RobotsRules(text) if text.strip() else None
        with self._lock:
            self._parsed[key] = (now, rules)
        return rules
