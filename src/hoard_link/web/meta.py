"""What a page says about itself: metadata, JSON-LD, feed links and icons (standard library only).

* :func:`page_meta` — ``{title, description, canonical, lang, site_name, author, published, image, favicon,
  keywords, og, twitter, properties}`` with relative URLs resolved. Attribute order does not matter
  (``<meta content=".." property="..">`` works), entities are decoded, ``<base href>`` is honoured.
* :func:`jsonld_blocks` / :func:`jsonld_nodes` — schema.org JSON-LD that survives real pages: ``<!-- -->`` and
  ``CDATA`` wrappers, trailing commas, raw control characters, HTML-escaped quotes; nodes found through
  ``@graph``, ``ItemList``/``itemListElement``, ``hasVariant``, ``mainEntity`` and plain nesting.
* :func:`discover_feeds` — the ``<link rel="alternate">`` feeds of a page.
* :func:`favicon_candidates` — the icon URLs worth trying, in order.

The Node twin is ``js/hoard-commons/web.js`` (``pageMeta``, ``jsonldBlocks``, ``jsonldNodes``, ``discoverFeeds``),
checked against ``tests/vectors/web_meta.json``.

This module replaces: Tantalus ``extract/jsonld.py`` (loader and walker), ``extract/meta.py`` (OpenGraph part),
Faustus ``content._extract_meta/_extract_og_image`` and ``favicon_routes._extract_icon_link``, Vitruvius
``references._meta_from_html``, Cook's JSON-LD loader, Links ``extract.metaContent``, Writers' meta script.
"""

from __future__ import annotations

import html as _html
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urljoin, urlsplit

__all__ = ["page_meta", "jsonld_blocks", "jsonld_nodes", "discover_feeds", "favicon_candidates", "load_jsonld"]


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.base_href = ""
        self.title = ""
        self.h1 = ""
        self.metas: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.scripts: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._script: Optional[list[str]] = None
        self._title_done = False
        self._h1_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        d = {k.lower(): ("" if v is None else v) for k, v in attrs}
        if tag == "html":
            self.lang = self.lang or d.get("lang", "") or d.get("xml:lang", "")
        elif tag == "base":
            self.base_href = self.base_href or d.get("href", "")
        elif tag == "meta":
            self.metas.append(d)
        elif tag == "link":
            self.links.append(d)
        elif tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True
        elif tag == "script" and re.search(r"ld\+json", d.get("type", ""), re.I):
            self._script = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title, self._title_done = False, True
        elif tag == "h1" and self._in_h1:
            self._in_h1, self._h1_done = False, True
        elif tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data


def _scan(html: Any) -> _Scan:
    s = _Scan()
    try:
        s.feed("" if html is None else str(html))
        s.close()
    except Exception:                                    # noqa: BLE001 - keep what was read
        pass
    return s


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _abs(href: str, base: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.lower().startswith(("javascript:", "data:", "vbscript:")):
        return ""
    try:
        return urljoin(base, href) if base else href
    except ValueError:
        return href


def _http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _base(scan: _Scan, base_url: str) -> str:
    if not scan.base_href:
        return base_url
    return _abs(scan.base_href, base_url) or base_url


# ---- JSON-LD ----------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_trailing_commas(s: str) -> str:
    """Remove ``,`` that precede ``}`` or ``]`` outside strings."""
    out: list[str] = []
    in_str = esc = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                pass                                    # drop it
            else:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def load_jsonld(raw: Any) -> tuple[Optional[Any], str]:
    """Parse one JSON-LD payload leniently: ``(value, "")`` or ``(None, error)``."""
    text = ("" if raw is None else str(raw)).lstrip("﻿").strip()
    if not text:
        return None, "empty block"
    text = re.sub(r"^\s*<!--", "", text)
    text = re.sub(r"-->\s*$", "", text).strip()
    m = re.match(r"^(?://\s*)?<!\[CDATA\[(.*?)(?://\s*)?\]\]>$", text, re.S)       # also the JavaScript-comment form
    if m:
        text = m.group(1).strip()
    text = _CONTROL.sub(" ", text)
    attempts = [text, _strip_trailing_commas(text)]
    if "&quot;" in text or "&#34;" in text:
        unescaped = _html.unescape(text)
        attempts += [unescaped, _strip_trailing_commas(unescaped)]
    error = ""
    for candidate in attempts:
        try:
            return json.loads(candidate, strict=False), ""
        except ValueError as exc:
            error = error or str(exc)
    return None, error[:120]


def jsonld_blocks(html: Any) -> tuple[list[Any], list[str]]:
    """``(payloads, errors)``: every ``<script type="application/ld+json">`` that parses, and one message per
    block that did not (``len(errors)`` is the number of unusable blocks)."""
    return _blocks_from_scripts(_scan(html).scripts)


def _blocks_from_scripts(scripts: list[str]) -> tuple[list[Any], list[str]]:
    payloads: list[Any] = []
    errors: list[str] = []
    for i, raw in enumerate(scripts, start=1):
        value, error = load_jsonld(raw)
        if error:
            errors.append(f"block {i}: {error}")
        else:
            payloads.append(value)
    return payloads, errors


_DEFAULT_SKIP = frozenset({"review", "reviews", "aggregaterating", "isrelatedto", "issimilarto", "isaccessoryorsparepartfor",
                           "isconsumablefor", "mainentityofpage", "potentialaction", "breadcrumb", "publisher", "author"})


def _types(node: dict) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower() for v in values if v}


def jsonld_nodes(blocks: Any, types: Optional[Iterable[str]] = None, *, skip: Iterable[str] = _DEFAULT_SKIP) -> Iterator[dict]:
    """Every typed JSON-LD node, parents before children, found through ``@graph``, lists, ``ItemList`` elements,
    ``hasVariant``, ``mainEntity`` and ordinary nesting. ``types`` (names such as ``"Product"``, ``"Recipe"``,
    ``"JobPosting"``; case-insensitive, any schema.org prefix) keeps only matching nodes. Keys in ``skip`` (reviews,
    related products, the publisher and author sub-objects ...) are not descended into."""
    wanted = {str(t).rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower() for t in types} if types is not None else None
    skipped = {k.lower() for k in skip}
    stack: list[tuple[Any, int]] = [(b, 0) for b in reversed(blocks if isinstance(blocks, list) else [blocks])]
    while stack:
        node, depth = stack.pop()
        if depth > 14:
            continue
        if isinstance(node, list):
            stack.extend((x, depth + 1) for x in reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        kinds = _types(node)
        if kinds and (wanted is None or kinds & wanted):
            yield node
        children = [v for k, v in node.items() if k.lower() not in skipped and not (k.startswith("@") and k not in ("@graph", "@list"))
                    and isinstance(v, (dict, list))]
        stack.extend((c, depth + 1) for c in reversed(children))


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        return next((t for t in (_first_text(v) for v in value) if t), "")
    if isinstance(value, dict):
        return _first_text(value.get("name") or value.get("@id") or value.get("value") or "")
    return "" if value is None else _clean(_html.unescape(str(value)))


# ---- feeds and icons --------------------------------------------------------------------------

_FEED_TYPES = {"application/rss+xml": "rss", "application/atom+xml": "atom", "application/feed+json": "json",
               "application/json": "json", "text/xml": "rss", "application/xml": "rss"}


def discover_feeds(html: Any, base_url: str = "") -> list[dict[str, str]]:
    """Feeds a page advertises: ``[{"url", "type", "kind", "title"}]`` (``kind`` is rss, atom or json), in document
    order, absolute, de-duplicated. Plain ``application/xml`` and ``text/xml`` links count only when the link or
    its title mentions a feed."""
    scan = _scan(html)
    base = _base(scan, base_url)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in scan.links:
        rel = link.get("rel", "").lower().split()
        mime = link.get("type", "").lower().split(";")[0].strip()
        if "alternate" not in rel and "feed" not in rel:
            continue
        kind = _FEED_TYPES.get(mime)
        if not kind:
            continue
        href = link.get("href", "")
        if mime in ("application/xml", "text/xml", "application/json") and not re.search(r"rss|atom|feed|xml", href + " " + link.get("title", ""), re.I):
            continue
        url = _abs(href, base)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "type": mime, "kind": kind, "title": _clean(link.get("title", ""))})
    return out


def _size(link: dict[str, str]) -> int:
    m = re.match(r"(\d+)x(\d+)", link.get("sizes", "").lower())
    return int(m.group(1)) * int(m.group(2)) if m else 0


def favicon_candidates(html: Any, base_url: str = "", *, ico_first: bool = True) -> list[str]:
    """Icon URLs to try, absolute http(s) and de-duplicated. Default order (the one the Faustus favicon proxy uses):
    ``/favicon.ico`` of the site, then the ``<link rel="icon">`` / ``shortcut icon`` entries in document order, then
    ``apple-touch-icon`` entries by size. ``ico_first=False`` puts the declared icons before ``/favicon.ico``."""
    scan = _scan(html)
    base = _base(scan, base_url)
    parts = urlsplit(base_url) if base_url else None
    default = f"{parts.scheme}://{parts.netloc}/favicon.ico" if parts and parts.scheme and parts.netloc else ""
    declared: list[str] = []
    touch: list[tuple[int, str]] = []
    for link in scan.links:
        rel = link.get("rel", "").lower().split()
        if not link.get("href"):
            continue
        url = _abs(link["href"], base)
        if not _http(url):
            continue
        if any(r.startswith("apple-touch-icon") for r in rel):
            touch.append((-_size(link), url))
        elif "icon" in rel:
            declared.append(url)
    touch_urls = [u for _, u in sorted(touch, key=lambda t: t[0])]
    order = ([default] + declared + touch_urls) if ico_first else (declared + [default] + touch_urls)
    out: list[str] = []
    for u in order:
        if u and u not in out:
            out.append(u)
    return out


# ---- page metadata ----------------------------------------------------------------------------

_IMAGE_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")
_PUBLISHED_KEYS = ("article:published_time", "og:article:published_time", "datepublished", "date", "pubdate", "publishdate",
                   "publish-date", "dc.date", "dc.date.issued", "sailthru.date", "parsely-pub-date", "article:modified_time")
_AUTHOR_KEYS = ("author", "article:author", "dc.creator", "twitter:creator", "parsely-author")


def _image_ok(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return _http(url) and not path.endswith((".svg", ".ico"))


def page_meta(html: Any, base_url: str = "") -> dict[str, Any]:
    """Metadata of a page (see the module docstring). Missing values are ``""`` (``keywords`` is ``[]``)."""
    scan = _scan(html)
    base = _base(scan, base_url)
    named: dict[str, str] = {}              # name / itemprop -> first content
    props: dict[str, str] = {}              # property -> first content
    for m in scan.metas:
        content = _clean(m.get("content", ""))
        if not content and "charset" in m:
            continue
        for attr, bucket in (("property", props), ("name", named), ("itemprop", named), ("http-equiv", named)):
            key = m.get(attr, "").strip().lower()
            if key and content and key not in bucket:
                bucket[key] = content
    both = {**named, **props}
    og = {k[3:]: v for k, v in props.items() if k.startswith("og:")}
    for k, v in named.items():                       # twitter cards and some og tags use name=
        if k.startswith("og:") and k[3:] not in og:
            og[k[3:]] = v
    twitter = {k[8:]: v for k, v in {**named, **props}.items() if k.startswith("twitter:")}
    other = {k: v for k, v in props.items() if not k.startswith(("og:", "twitter:"))}

    def first(*keys: str) -> str:
        return next((both[k] for k in keys if both.get(k)), "")

    title = first("og:title", "twitter:title") or _clean(scan.title) or _clean(scan.h1)
    description = first("og:description", "twitter:description", "description")
    canonical = ""
    for link in scan.links:
        if "canonical" in link.get("rel", "").lower().split() and link.get("href"):
            canonical = _abs(link["href"], base)
            break
    canonical = canonical or _abs(first("og:url"), base)
    lang = _clean(scan.lang) or first("og:locale", "content-language")
    site_name = first("og:site_name", "application-name", "apple-mobile-web-app-title")
    author = first(*_AUTHOR_KEYS)
    published = first(*_PUBLISHED_KEYS)
    blocks, _ = _blocks_from_scripts(scan.scripts)
    if blocks and (not author or not published or not description):
        for node in jsonld_nodes(blocks, {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle", "ScholarlyArticle",
                                          "WebPage", "Recipe", "VideoObject"}, skip=()):
            author = author or _first_text(node.get("author"))
            published = published or _first_text(node.get("datePublished"))
            description = description or _first_text(node.get("description"))
            if author and published:
                break
    image = ""
    for key in _IMAGE_KEYS:
        candidate = _abs(both.get(key, ""), base)
        if candidate and _image_ok(candidate):
            image = candidate
            break
    if not image:
        for link in scan.links:
            if "image_src" in link.get("rel", "").lower().split():
                candidate = _abs(link.get("href", ""), base)
                if candidate and _image_ok(candidate):
                    image = candidate
                    break
    icons = favicon_candidates(html, base_url, ico_first=False)
    declared_icon = next((u for u in icons if not u.endswith("/favicon.ico")), "")
    favicon = declared_icon or (icons[0] if icons else "")
    keywords = [k.strip() for k in re.split(r"[,;]", named.get("keywords", "")) if k.strip()]
    return {"title": title, "description": description, "canonical": canonical, "lang": lang, "site_name": site_name,
            "author": author, "published": published, "image": image, "favicon": favicon, "keywords": keywords,
            "og": og, "twitter": twitter, "properties": other}

