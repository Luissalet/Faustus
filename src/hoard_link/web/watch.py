"""Change detection for pages and feeds: the decision logic, with no network and no storage.

The caller fetches (with :mod:`.fetch`, or anything that yields the same fields), keeps the small ``state`` dict this
module returns, and passes it back next time. Semantics (from the Tantalus information sentry):

* the first check of a page or feed is a **baseline**: no finding, the state is recorded;
* a **blocked** answer (anti-bot page, login wall, 403/429, an unusable "page" that merely talks about CAPTCHAs or
  JavaScript) never counts as a change and never replaces the stored state: the old text and hash stay, ``error``
  says what happened. A challenge page served with status 200 is recognised too;
* ``304`` / ``not_modified`` keeps the state and reports nothing;
* clocks, "5 minutes ago", ISO stamps, long ids and ``©`` lines are ignored (:func:`~.htmltext.normalise_for_hash`);
* lines are compared as sets, so the same lines in another order is not a change;
* a feed reports only items whose id is new, at most :data:`MAX_FEED_FINDINGS`.

The Node twin is ``checkPage`` in ``js/hoard-commons/web.js`` (vectors: ``tests/vectors/web_watch.json``).
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from .blocks import block_hint, detect_block
from .htmltext import content_hash, normalise_for_hash, quality, readable
from .feeds import parse_feed

__all__ = ["check_page", "check_feed", "check_feed_result", "diff_lines", "diff_summary", "MAX_STORED_TEXT", "MAX_SEEN",
           "MAX_FEED_FINDINGS"]

MAX_STORED_TEXT = 20_000
MAX_SEEN = 400
MAX_FEED_FINDINGS = 20
MAX_ADDED_LINES = 8
MAX_LINE_CHARS = 200


def _get(fr: Any, name: str, default: Any = "") -> Any:
    if isinstance(fr, Mapping):
        value = fr.get(name, default)
    else:
        value = getattr(fr, name, default)
    return default if value is None else value


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def diff_lines(old: str, new: str) -> tuple[list[str], list[str]]:
    """``(added, removed)`` lines of two texts, in document order, compared on their normalised form (volatile lines
    and re-ordered lines do not count)."""
    old_lines = [l for l in old.splitlines() if normalise_for_hash(l)]
    new_lines = [l for l in new.splitlines() if normalise_for_hash(l)]
    old_set = {normalise_for_hash(l) for l in old_lines}
    new_set = {normalise_for_hash(l) for l in new_lines}
    return ([l for l in new_lines if normalise_for_hash(l) not in old_set],
            [l for l in old_lines if normalise_for_hash(l) not in new_set])


def diff_summary(added: list[str], removed: list[str]) -> str:
    out = [f"+ {_clip(l, MAX_LINE_CHARS)}" for l in added[:MAX_ADDED_LINES]]
    if len(added) > MAX_ADDED_LINES:
        out.append(f"+ … {len(added) - MAX_ADDED_LINES} more lines")
    if removed:
        out.append(f"- {len(removed)} line(s) removed: {_clip(removed[0], 120)}")
    return "\n".join(out)


def check_page(fetch: Any, prev: Optional[Mapping[str, Any]] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Decide whether a fetched page changed. ``fetch`` is a :class:`~.fetch.FetchResult` or a dict with the same
    field names (``text``, ``ok``, ``not_modified``, ``blocked``, ``block_reason``, ``status``, ``headers``,
    ``etag``, ``last_modified``, ``final_url``, ``url``, ``error``). ``prev`` is the state returned last time
    (``{hash, text, etag, last_modified}``) or ``None``.

    Returns ``(finding, state)``. ``finding`` is ``None`` or ``{kind: "page_change", url, title, snippet, added,
    removed, summary, content_hash}``. ``state`` is ``{hash, text, etag, last_modified, error}``; store it whole."""
    prev = dict(prev or {})
    state = {"hash": str(prev.get("hash") or ""), "text": str(prev.get("text") or ""), "etag": str(prev.get("etag") or ""),
             "last_modified": str(prev.get("last_modified") or ""), "error": ""}
    url = str(_get(fetch, "final_url") or _get(fetch, "url") or "")
    if _get(fetch, "not_modified", False) or int(_get(fetch, "status", 0) or 0) == 304:
        return None, state
    text_html = str(_get(fetch, "text"))
    reason = str(_get(fetch, "block_reason")) if _get(fetch, "blocked", False) else ""
    if not reason and text_html:
        reason = detect_block(int(_get(fetch, "status", 200) or 200), text_html, _get(fetch, "headers", {}) or {}, url)
        if reason == "http_5xx":
            reason = ""
    if reason:
        state["error"] = f"blocked: {block_hint(reason)}"
        return None, state
    if not _get(fetch, "ok", False):
        state["error"] = str(_get(fetch, "error") or (f"HTTP {_get(fetch, 'status')}" if _get(fetch, "status") else "no answer"))
        return None, state
    title, text = readable(text_html)
    problem = quality(text)
    if problem:
        state["error"] = f"low quality page ({problem}); no comparison made"
        return None, state
    new_hash = content_hash(text)
    stored = text[:MAX_STORED_TEXT]
    old_hash, old_text = state["hash"], state["text"]
    state.update(hash=new_hash, text=stored, etag=str(_get(fetch, "etag")), last_modified=str(_get(fetch, "last_modified")))
    if not old_hash or new_hash == old_hash:
        return None, state                       # baseline, or nothing changed
    added, removed = diff_lines(old_text, stored)
    if not added and not removed:
        return None, state                       # the same lines in another order
    finding = {
        "kind": "page_change", "url": url, "title": title or url,
        "snippet": _clip(" ".join(added[:3]) or "content removed", 300),
        "added": added[:50], "removed": removed[:50], "summary": diff_summary(added, removed),
        "content_hash": _sha(url + "\n" + "\n".join(normalise_for_hash(l) for l in (added or removed)))[:32],
    }
    return finding, state


def _key(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("link") or item.get("title") or "")


def check_feed(feed: Any, seen: Optional[list[str]] = None, baseline: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """New items of a parsed feed. ``feed`` is the dict of :func:`~.feeds.parse_feed` (or a plain list of item
    dicts), ``seen`` the ids already reported, ``baseline`` ``True`` on the first check (nothing is reported, every id is
    remembered). Returns ``(new_items, seen)`` — the new items in feed order (at most 20) and the updated seen list
    (current ids first, then older ones, at most 400)."""
    items = feed.get("items", []) if isinstance(feed, Mapping) else list(feed or [])
    seen = list(seen or [])
    if not items:
        return [], seen
    keys = [_key(i) for i in items]
    seen_set = set(seen)
    current = set(keys)
    merged = ([k for k in keys if k] + [k for k in seen if k not in current])[:MAX_SEEN]
    if baseline:
        return [], merged
    fresh = [dict(i, key=k) for i, k in zip(items, keys) if k and k not in seen_set]
    return fresh[:MAX_FEED_FINDINGS], merged


def check_feed_result(fetch: Any, seen: Optional[list[str]] = None, baseline: bool = False,
                      base_url: str = "") -> tuple[list[dict[str, Any]], list[str], str]:
    """:func:`check_feed` over a fetch result: ``(new_items, seen, error)``. A blocked, failed or not-a-feed answer
    returns no items, the old ``seen`` list and an ``error`` text; ``not_modified`` returns no items and no error."""
    seen = list(seen or [])
    if _get(fetch, "not_modified", False) or int(_get(fetch, "status", 0) or 0) == 304:
        return [], seen, ""
    if _get(fetch, "blocked", False):
        return [], seen, f"blocked: {block_hint(str(_get(fetch, 'block_reason')))}"
    if not _get(fetch, "ok", False):
        return [], seen, str(_get(fetch, "error") or "no answer")
    feed = parse_feed(str(_get(fetch, "text")), base_url or str(_get(fetch, "final_url") or _get(fetch, "url")))
    if not feed or not feed["items"]:
        return [], seen, "not a feed or empty"
    items, merged = check_feed(feed, seen, baseline)
    return items, merged, ""
