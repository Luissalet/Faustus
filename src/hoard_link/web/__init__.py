"""Web access for every Hoard app, written once.

Standard library only at import time (``httpx`` and ``playwright`` are imported inside the functions that
need them). The Node twin is ``js/hoard-commons/web.js``.

Pure modules (no network)
    :mod:`.urls`      normalise, key, unwrap and clean URLs; host and registrable domain
    :mod:`.safety`    SSRF policy: profiles, IP and URL checks, DNS-pinned transport
    :mod:`.blocks`    anti-bot / login-wall detection over a fetched document
    :mod:`.htmltext`  HTML to readable text, to Markdown, volatile-text normalisation and hashing
    :mod:`.meta`      page metadata, JSON-LD, feed discovery, favicon candidates
    :mod:`.feeds`     RSS 1.0 / 2.0 and Atom parsing, GitHub feeds, news RSS
    :mod:`.watch`     page-change and feed-change decisions
    :mod:`.robots`    robots.txt rules and a cache with an injectable fetcher and clock

Network modules
    :mod:`.fetch`     the polite fetcher (SSRF check per hop, robots, throttle, cooldown, cache, retries)
    :mod:`.browser`   the optional Playwright rung (one profile, one worker thread)
    :mod:`.search`    web search over several engines with rank fusion

Import the module you need (``from hoard_link.web import urls``); nothing is imported eagerly.
"""

__all__ = ["urls", "safety", "blocks", "htmltext", "meta", "feeds", "watch", "robots", "fetch", "browser", "search"]
