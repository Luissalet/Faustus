"""RSS 1.0 (RDF), RSS 2.0 and Atom feeds, plus the small feed helpers apps kept re-writing.

* :func:`parse_feed` — ``{format, title, link, description, items: [...]}`` or ``None`` when the text is not a feed.
  Item fields: ``id, title, link, published, updated, date, summary, content, author``. Dates are ISO 8601 in UTC
  (``2026-10-02T09:30:00Z``; ``""`` when absent or unreadable). ``summary`` is plain text (at most 400 characters),
  ``content`` is the feed's own HTML (``content:encoded`` / Atom ``content``) for :mod:`.htmltext` to convert.
  ``published`` and ``updated`` are independent (an Atom entry with both keeps both).
* Safety: a document that declares entities (``<!ENTITY``) is refused (billion-laughs and external entities); a BOM
  is stripped; stray control characters and bare ``&`` are repaired once before giving up.
* :func:`github_feed` — the Atom feed of a GitHub repository (releases, tags or commits).
* :func:`parse_news_rss` — Google News / Bing News result feeds as search hits.
* :func:`discover_feeds` — re-exported from :mod:`.meta`.

The Node twin (``parseFeed``) is a string scanner, not a regex pile: CDATA and nested tags are handled; both are
checked against ``tests/vectors/web_feeds.json``.

This module replaces: Tantalus ``info.parse_feed`` and ``search.parse_news_rss``, Links ``watches.js::parseFeed`` /
``discoverFeed`` / ``githubFeed``, Faustus ``reach/rss.py`` (whose Atom ``updated or published`` test dropped the
date because an ``Element`` with text but no children is falsy).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

from .meta import discover_feeds
from .urls import split_url

__all__ = ["parse_feed", "github_feed", "parse_news_rss", "discover_feeds", "to_iso_utc"]

MAX_ITEMS = 200
_ENTITY_DECL = re.compile(r"<!ENTITY", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BARE_AMP = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)")
_TAGS = re.compile(r"<[^>]+>")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")


def _repair_entities(text: str) -> str:
    """HTML entities that XML does not know (``&nbsp;``, ``&eacute;``) become their characters; bare ``&`` is escaped."""
    def known(m: "re.Match[str]") -> str:
        if m.group(1) in _XML_ENTITIES:
            return m.group(0)
        char = _html.unescape(m.group(0))
        return m.group(0) if char == m.group(0) else char.replace("&", "&amp;").replace("<", "&lt;")
    return _BARE_AMP.sub("&amp;", _NAMED_ENTITY.sub(known, _CONTROL.sub(" ", text)))


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _parse_xml(xml: Any) -> Optional[ET.Element]:
    if xml is None:
        return None
    if isinstance(xml, bytes):
        if xml.startswith(b"\xef\xbb\xbf"):
            xml = xml[3:]
        if _ENTITY_DECL.search(xml.decode("latin-1", "ignore")):
            return None
        data: Any = xml.lstrip()
    else:
        text = str(xml).lstrip("﻿").lstrip()
        if not text or _ENTITY_DECL.search(text):
            return None
        data = text
    for attempt in range(2):
        try:
            return ET.fromstring(data)
        except (ET.ParseError, ValueError):
            if attempt:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            data = _repair_entities(data)
    return None


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAGS.sub(" ", text or "")).replace("\xa0", " ")).strip()


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def to_iso_utc(value: Any) -> str:
    """An RFC 822 or ISO 8601 date as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC; no zone means UTC); ``""`` when unreadable."""
    text = ("" if value is None else str(value)).strip()
    if not text:
        return ""
    dt: Optional[_dt.datetime] = None
    iso = text.replace("Z", "+00:00").replace("z", "+00:00")
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(el: ET.Element, *names: str) -> str:
    """Text of the first direct child whose local name is in ``names`` (in the order given)."""
    for name in names:
        for child in el:
            if _local(child.tag) == name:
                value = "".join(child.itertext()).strip()
                if value:
                    return value
    return ""


def _atom_link(el: ET.Element, base: str) -> str:
    chosen = ""
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if not href:
            continue
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate":
            return urljoin(base, href) if base else href
        chosen = chosen or (urljoin(base, href) if base else href)
    return chosen


def _item(el: ET.Element, atom: bool, base: str) -> Optional[dict[str, str]]:
    if atom:
        link = _atom_link(el, base)
    else:
        link = _text(el, "link")
        if not link:                                           # RSS 2.0 permalink guid, or an atom:link inside RSS
            guid = el.find("guid")
            if guid is not None and (guid.text or "").strip().lower().startswith("http") and guid.attrib.get("isPermaLink", "true") != "false":
                link = guid.text.strip()
            else:
                link = _atom_link(el, base)
        link = urljoin(base, link) if base and link else link
    title = _plain(_text(el, "title"))
    ident = _text(el, "guid", "id") or link or title
    summary_src = _text(el, "summary", "description") or _text(el, "encoded", "content")
    content = _text(el, "encoded", "content") or (_text(el, "description") if not atom else "")
    published = to_iso_utc(_text(el, "pubdate", "published", "date", "issued"))
    updated = to_iso_utc(_text(el, "updated", "modified"))
    author = ""
    for child in el:
        if _local(child.tag) in ("author", "creator"):
            author = _text(child, "name") or "".join(child.itertext()).strip()
            if author:
                break
    author = _plain(author) or _plain(_text(el, "author", "creator"))
    if not (ident or link):
        return None
    return {"id": ident, "title": _clip(title, 300), "link": link, "published": published, "updated": updated,
            "date": published or updated, "summary": _clip(_plain(summary_src), 400), "content": content[:50_000],
            "author": _clip(author, 120)}


def parse_feed(xml: Any, base_url: str = "", *, max_items: int = MAX_ITEMS) -> Optional[dict[str, Any]]:
    """Parse a feed (see the module docstring). ``None`` when the text is not RSS, RDF or Atom, is empty, or
    declares entities. An empty feed (no items) is still a feed: ``{"items": []}``."""
    root = _parse_xml(xml)
    if root is None:
        return None
    kind = _local(root.tag)
    if kind == "feed":
        fmt, atom = "atom", True
        container = root
        link = _atom_link(root, base_url)
    elif kind == "rss" or kind == "channel":
        fmt, atom = "rss", False
        container = root.find("channel") if kind == "rss" else root
        if container is None:
            container = root
        link = _text(container, "link")
    elif kind == "rdf":
        fmt, atom = "rdf", False
        container = next((c for c in root if _local(c.tag) == "channel"), root)
        link = _text(container, "link")
    else:
        return None
    items: list[dict[str, str]] = []
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            parsed = _item(el, atom or _local(el.tag) == "entry", base_url)
            if parsed:
                items.append(parsed)
                if len(items) >= max_items:
                    break
    return {"format": fmt, "title": _plain(_text(container, "title")), "link": urljoin(base_url, link) if base_url and link else link,
            "description": _clip(_plain(_text(container, "description", "subtitle")), 400), "items": items}


# ---- GitHub -----------------------------------------------------------------------------------

_GITHUB_RESERVED = frozenset("""orgs users topics marketplace sponsors settings features pricing about search explore
notifications pulls issues gist new login join collections trending enterprise customer-stories readme apps""".split())


def github_feed(url: Any, what: str = "releases") -> Optional[dict[str, str]]:
    """The Atom feed that tracks a GitHub repository: ``{"repo", "url", "what", "name"}``, or ``None`` when ``url``
    is not a repository URL. A path hint in the URL (``/commits/<branch>``, ``/tags``, ``/releases``) wins over
    ``what`` (``"releases"``, ``"tags"`` or ``"commits"``)."""
    p = split_url(str(url or "").strip())
    if p is None or p.scheme not in ("http", "https") or p.host.lower() not in ("github.com", "www.github.com"):
        return None
    segs = [s for s in p.path.split("/") if s]
    if len(segs) < 2 or segs[0].lower() in _GITHUB_RESERVED:
        return None
    owner, repo = segs[0], re.sub(r"\.git$", "", segs[1])
    if not repo:
        return None
    rest = segs[2:]
    kind = what if what in ("releases", "tags", "commits") else "releases"
    branch = ""
    if rest and rest[0] in ("commits", "tags", "releases"):
        kind = rest[0]
        if kind == "commits" and len(rest) > 1:
            branch = "/".join(rest[1:]).removesuffix(".atom")
    elif rest and rest[0] == "tree" and kind == "commits" and len(rest) > 1:
        branch = "/".join(rest[1:])
    feed = f"https://github.com/{owner}/{repo}/{kind}" + (f"/{branch}" if branch else "") + ".atom"
    return {"repo": f"{owner}/{repo}", "url": feed, "what": kind, "name": f"{owner}/{repo} {kind}"}


# ---- news RSS ---------------------------------------------------------------------------------

def _bing_news_target(href: str) -> str:
    """Bing News RSS links are ``apiclick`` redirects; the article is in the ``url`` parameter."""
    try:
        parts = urlsplit(href)
        if "bing.com" in (parts.hostname or "") and "apiclick" in parts.path:
            target = parse_qs(parts.query).get("url", [""])[0]
            if target.startswith("http"):
                return target
    except ValueError:
        pass
    return href


def parse_news_rss(xml: Any, engine: str = "gnews") -> list[dict[str, Any]]:
    """Items of a news RSS feed (``engine`` ``"gnews"`` or ``"bingnews"``) as search hits
    ``{url, title, snippet, engine, rank, published}``. Google News titles end with `` - <publisher>`` and its
    ``<source url=...>`` names the publisher's site (the article link itself is a redirect), which is appended to the
    snippet as ``[https://publisher]``; Bing News ``apiclick`` links are unwrapped."""
    root = _parse_xml(xml)
    if root is None:
        return []
    hits: list[dict[str, Any]] = []
    for rank, item in enumerate((e for e in root.iter() if _local(e.tag) == "item"), start=1):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        if engine == "bingnews":
            link = _bing_news_target(link)
        source = item.find("source")
        publisher = (source.text or "").strip() if source is not None and source.text else ""
        publisher_url = source.get("url", "") if source is not None else ""
        desc = _plain(item.findtext("description") or "")
        base_title = title.rsplit(" - ", 1)[0] if publisher and title.endswith(" - " + publisher) else title
        snippet = "" if (not desc or desc == title or desc.startswith(base_title)) else desc
        if publisher and not snippet:
            snippet = publisher
        snippet = snippet[:400]
        if publisher_url:
            snippet = (snippet + f" [{publisher_url}]").strip()
        hits.append({"url": link, "title": title, "snippet": snippet, "engine": engine, "rank": rank,
                     "published": to_iso_utc(item.findtext("pubDate")) or None})
    return hits
