"""HTML to text and Markdown with the standard library only, plus the text hygiene change detection needs.

* :func:`readable` — ``(title, text)``: what a person would call the content. Scripts, styles, hidden nodes,
  navigation, footers, asides, forms and cookie banners are dropped; ``main`` (or a single ``article``) is
  preferred. Block structure survives as lines, table cells do not glue together.
* :func:`to_markdown` — headings, nested lists, GFM tables, fenced code with its language, inline emphasis and
  code, links resolved to absolute URLs; also ``links``, ``headings`` and the table count.
* :func:`quality` — ``""`` when a text is a usable document, otherwise why not (blocked page, too short, mostly
  navigation).
* :func:`normalise_for_hash` / :func:`content_hash` — a text with the volatile noise removed (clocks, ISO
  stamps, "5 minutes ago", ``©`` lines, long hex tokens) so a re-render does not look like a change.
* :func:`excerpt`, :func:`markdown_to_text`, :func:`parse_html` (the small DOM the rest is built on).

Entities are decoded with :func:`html.unescape` (named, decimal and hex). The Node twin of :func:`readable`,
:func:`normalise_for_hash` and :func:`content_hash` is in ``js/hoard-commons/web.js``.

This module replaces: Tantalus ``extract/text.py`` and ``info.readable_text``/``quality_gate``, Faustus
``html_markdown.py``, Babels ``html_to_markdown.py``, the regex text strippers of Cook and JobHunters.
"""

from __future__ import annotations

import hashlib
import html as _html
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlparse

from ..text import clamp_text

__all__ = [
    "Node", "parse_html", "readable", "visible_text", "to_markdown", "markdown_to_text", "quality", "chrome_ratio",
    "normalise_for_hash", "content_hash", "excerpt", "MIN_WORDS",
]

MIN_WORDS = 40
MAX_DEPTH = 200

# ---------------------------------------------------------------------------------------------- DOM
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_CLOSES_P = frozenset("""address article aside blockquote details div dl fieldset figcaption figure footer form h1 h2 h3 h4
h5 h6 header hr main nav ol p pre section table ul""".split())
_IMPLIED_CLOSE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist"})),
}
_INLINE = frozenset("""a abbr b bdi bdo big cite code data del dfn em font i img ins kbd label mark q s samp small span strike
strong sub sup time tt u var wbr br""".split())


class Node:
    """A tiny DOM node: an element (``tag`` is its lowercase name) or a text node (``tag == "#text"``)."""

    __slots__ = ("tag", "attrs", "children", "parent", "text", "_classes")

    def __init__(self, tag: str, attrs: Optional[dict[str, str]] = None, parent: Optional["Node"] = None, text: str = ""):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parent = parent
        self.text = text
        self._classes: Optional[frozenset[str]] = None

    @property
    def is_text(self) -> bool:
        return self.tag == "#text"

    @property
    def classes(self) -> frozenset[str]:
        if self._classes is None:
            self._classes = frozenset(self.attrs.get("class", "").lower().split())
        return self._classes

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    def iter(self) -> Iterator["Node"]:
        """This node and every descendant, document order."""
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def find(self, tag: str) -> Optional["Node"]:
        return next((n for n in self.iter() if n.tag == tag), None)

    def find_all(self, tag: str) -> list["Node"]:
        return [n for n in self.iter() if n.tag == tag]

    def ancestors(self) -> Iterator["Node"]:
        n = self.parent
        while n is not None:
            yield n
            n = n.parent


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def _top(self) -> Node:
        return self.stack[-1]

    def _pop_to(self, index: int) -> None:
        del self.stack[index:]

    def _close_implied(self, tag: str) -> None:
        if tag in _CLOSES_P:
            for i in range(len(self.stack) - 1, 0, -1):
                name = self.stack[i].tag
                if name == "p":
                    self._pop_to(i)
                    break
                if name not in _INLINE:
                    break
        rule = _IMPLIED_CLOSE.get(tag)
        if rule:
            closers, stoppers = rule
            for i in range(len(self.stack) - 1, 0, -1):
                name = self.stack[i].tag
                if name in closers:
                    self._pop_to(i)
                    break
                if name in stoppers:
                    break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        self._close_implied(tag)
        d: dict[str, str] = {}
        for k, v in attrs:
            d.setdefault(k.lower(), "" if v is None else v)
        node = Node(tag, d, self._top())
        self._top().children.append(node)
        if tag not in _VOID and len(self.stack) < MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _VOID or tag in ("path", "circle", "rect", "use", "line", "polygon", "polyline", "ellipse", "stop"):
            self.handle_starttag(tag, attrs)
            if tag not in _VOID and self.stack[-1].tag == tag:
                self.stack.pop()
        else:
            self.handle_starttag(tag, attrs)      # `<div/>` is an open tag in HTML

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._pop_to(i)
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        top = self._top()
        if top.children and top.children[-1].is_text:
            top.children[-1].text += data
        else:
            top.children.append(Node("#text", parent=top, text=data))


def parse_html(html: Any) -> Node:
    """Parse ``html`` into a :class:`Node` tree. Tolerant: unclosed and misnested tags are repaired the usual way
    (``p``, ``li``, table rows and cells close implicitly), comments are dropped, nesting is capped."""
    builder = _Builder()
    try:
        builder.feed("" if html is None else str(html))
        builder.close()
    except Exception:                                    # noqa: BLE001 - a broken page still yields what was read
        pass
    return builder.root


# ---------------------------------------------------------------------------------------------- shared rules
_ALWAYS_DROP = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "canvas", "head", "title", "meta",
                          "link", "dialog", "object", "embed", "base"})
_CHROME_TAGS = frozenset({"nav", "footer", "aside", "form", "header"})
_NOISE_TOKENS = re.compile(r"cookie|consent|onetrust|cookiebot|gdpr|cmp-|newsletter", re.I)
_HIDDEN_CLASS = frozenset({"hidden", "d-none", "is-hidden", "u-hidden", "hide", "sr-only", "visually-hidden", "is-template"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo"})
_BLOCK = frozenset("""address article aside blockquote body caption dd details div dl dt fieldset figcaption figure footer form
h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section summary table tbody tfoot thead tr ul menu
center""".split())
_SEPARATORS = frozenset({"td", "th"})


def _hidden(n: Node) -> bool:
    a = n.attrs
    if "hidden" in a or a.get("aria-hidden", "").lower() == "true" or a.get("type", "").lower() == "hidden":
        return True
    style = a.get("style")
    if style and _HIDDEN_STYLE.search(style):
        return True
    return bool(n.classes & _HIDDEN_CLASS)


def _is_chrome(n: Node) -> bool:
    name = n.tag
    if name == "header":
        # a header inside an article or main is the article's own title block
        return not any(p.tag in ("article", "main") for p in n.ancestors())
    if name in _CHROME_TAGS:
        return True
    if n.get("role").lower() in _CHROME_ROLES:
        return True
    if name in ("html", "body", "main", "article"):
        return False
    ident = n.get("id") + " " + n.get("class")
    return bool(ident.strip() and _NOISE_TOKENS.search(ident))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------------------------------------- readable text
_BREAK = object()


def _emit(n: Node, drop_chrome: bool, out: list, in_pre: bool = False) -> None:
    for c in n.children:
        if c.is_text:
            if in_pre:
                for i, line in enumerate(c.text.split("\n")):
                    if i:
                        out.append(_BREAK)
                    out.append(line)
            else:
                out.append(c.text)
            continue
        name = c.tag
        if name in _ALWAYS_DROP or _hidden(c) or (drop_chrome and _is_chrome(c)):
            continue
        if name in ("br", "hr"):
            out.append(_BREAK)
            continue
        block = name in _BLOCK
        if block:
            out.append(_BREAK)
        _emit(c, drop_chrome, out, in_pre or name == "pre")
        if block:
            out.append(_BREAK)
        elif name in _SEPARATORS:
            out.append(" ")


def _lines(out: list) -> list[str]:
    lines: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        line = _collapse("".join(cur))
        cur.clear()
        if line and (not lines or lines[-1] != line):
            lines.append(line)

    for piece in out:
        if piece is _BREAK:
            flush()
        else:
            cur.append(piece)
    flush()
    return lines


def _title_of(root: Node) -> str:
    t = root.find("title")
    if t is not None:
        title = _collapse("".join(x.text for x in t.iter() if x.is_text))
        if title:
            return title[:300]
    h1 = root.find("h1")
    return _collapse("".join(x.text for x in h1.iter() if x.is_text))[:300] if h1 is not None else ""


def _words(lines: list[str]) -> int:
    return sum(len(l.split()) for l in lines)


def _choose_root(doc: Node, drop_chrome: bool) -> Node:
    body = doc.find("body") or doc
    main = next((n for n in doc.iter() if n.tag == "main" or n.get("role").lower() == "main"), None)
    if main is not None:
        return main
    articles = doc.find_all("article")
    if len(articles) == 1:
        return articles[0]
    return body


def readable(html: Any, *, drop_chrome: bool = True) -> tuple[str, str]:
    """``(title, text)``. With ``drop_chrome=False`` only scripts, styles and hidden nodes are dropped (every
    visible word, for phrase rules that look at buttons and notes inside forms and headers)."""
    doc = parse_html(html)
    title = _title_of(doc)
    body = doc.find("body") or doc
    if not drop_chrome:
        out: list = []
        _emit(body, False, out)
        return title, "\n".join(_lines(out))
    root = _choose_root(doc, True)
    out = []
    _emit(root, True, out)
    lines = _lines(out)
    if root is not body and _words(lines) < MIN_WORDS:
        out = []
        _emit(body, True, out)
        lines = _lines(out)
    return title, "\n".join(lines)


def visible_text(html: Any) -> str:
    """Every visible word of a page (``readable(html, drop_chrome=False)[1]``)."""
    return readable(html, drop_chrome=False)[1]


# ---------------------------------------------------------------------------------------------- Markdown
_NOISE_TAGS_MD = frozenset({"script", "style", "noscript", "template", "nav", "footer", "aside", "form", "iframe", "svg",
                            "button", "select", "option", "textarea", "label", "input", "head", "dialog", "canvas"})
_NOISE_CLASSES = frozenset({"toc", "navbox", "vertical-navbox", "vector-toc", "mw-editsection", "mw-jump-link", "headerlink",
                            "anchorjs-link", "skip-link", "breadcrumbs", "breadcrumb", "sidebar", "cookie-banner"})
_PERMALINK = frozenset({"¶", "#", "§", "🔗"})
_CONTENT_CLASS = re.compile(r"content|main|body|article|post|entry|text", re.I)
_THIN = 600
_LINKS_CAP = 200
_HEADINGS_CAP = 50


def _md_skip(n: Node) -> bool:
    name = n.tag
    if name in _NOISE_TAGS_MD or _hidden(n):
        return True
    if name == "header":
        return _is_chrome(n)
    if n.get("role").lower() in _CHROME_ROLES or n.get("id").lower() == "toc" or (n.classes & _NOISE_CLASSES):
        return True
    if name == "a" and _collapse(_plain(n)) in _PERMALINK:
        return True
    return False


def _plain(n: Node, sep: str = "") -> str:
    """Text of a subtree without hidden or noisy parts."""
    parts: list[str] = []

    def rec(x: Node) -> None:
        for c in x.children:
            if c.is_text:
                parts.append(c.text)
            elif c.tag in _ALWAYS_DROP or _hidden(c):
                continue
            else:
                if sep and c.tag in _BLOCK:
                    parts.append(sep)
                rec(c)
                if sep and c.tag in _BLOCK:
                    parts.append(sep)

    rec(n)
    return "".join(parts)


def _text_len(n: Node) -> int:
    total = 0
    stack = list(n.children)
    while stack:
        c = stack.pop()
        if c.is_text:
            total += len(c.text.strip())
        elif not _md_skip(c):
            stack.extend(c.children)
    return total


def _matches(n: Node, sel: tuple[str, str, str, str]) -> bool:
    tag, id_, cls, role = sel
    return ((not tag or n.tag == tag) and (not id_ or n.get("id") == id_) and (not cls or cls in n.classes)
            and (not role or n.get("role").lower() == role))


_MAIN_SELECTORS = (("main", "", "", ""), ("", "", "", "main"), ("", "mw-content-text", "", ""), ("article", "", "", ""),
                   ("", "main-content", "", ""), ("", "content", "", ""), ("", "main", "", ""), ("", "", "md-content", ""),
                   ("", "", "markdown-body", ""), ("", "", "post-content", ""), ("", "", "entry-content", ""),
                   ("", "", "article-body", ""), ("", "", "documentwrapper", ""))


class _Ctx:
    def __init__(self, base_url: str):
        self.base = base_url
        self.links: list[dict] = []
        self.headings: list[dict] = []
        self.seen: set[tuple[str, str]] = set()
        self.tables = 0


def _resolve(href: str, base: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    low = href.lower()
    if low.startswith(("javascript:", "data:", "vbscript:")):
        return None
    if low.startswith(("mailto:", "tel:")):
        return href
    try:
        return urljoin(base, href) if base else href
    except ValueError:
        return href


def _internal(url: str, base: str) -> bool:
    if not base:
        return False
    try:
        return (urlparse(url).netloc or "").lower() == (urlparse(base).netloc or "").lower()
    except ValueError:
        return False


def _ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _has_block(n: Node) -> bool:
    return any((not c.is_text) and (c.tag in _BLOCK or c.tag in ("ul", "ol", "table", "pre") or _has_block(c)) for c in n.children)


def _is_inline(n: Node) -> bool:
    if n.is_text:
        return True
    return n.tag in _INLINE and not _has_block(n)


def _inline(n: Node, ctx: _Ctx) -> str:
    return _ws(_inline_raw(n.children, ctx))


def _inline_raw(nodes: list[Node], ctx: _Ctx) -> str:
    parts: list[str] = []
    for c in nodes:
        if c.is_text:
            parts.append(c.text)
            continue
        name = c.tag
        if name in ("ul", "ol") or _md_skip(c):
            continue
        if name in ("strong", "b"):
            inner = _inline(c, ctx)
            parts.append(f"**{inner}**" if inner else "")
        elif name in ("em", "i"):
            inner = _inline(c, ctx)
            parts.append(f"*{inner}*" if inner else "")
        elif name == "code":
            text = _plain(c).strip()
            parts.append(f"`{text}`" if text else "")
        elif name == "br":
            parts.append("\n")
        elif name == "a":
            href = c.get("href")
            text = _inline(c, ctx) or href.strip()
            url = _resolve(href, ctx.base)
            if not url or not text:
                parts.append(text)
            else:
                if len(ctx.links) < _LINKS_CAP and (text, url) not in ctx.seen:
                    ctx.seen.add((text, url))
                    ctx.links.append({"text": text, "url": url, "internal": _internal(url, ctx.base)})
                parts.append(f"[{text}]({url})")
        elif name == "img":
            alt, src = c.get("alt").strip(), c.get("src").strip()
            if alt and src:
                parts.append(f"![{alt}]({_resolve(src, ctx.base) or src})")
        elif name in _BLOCK:
            parts.append(" " + _inline_raw(c.children, ctx) + " ")
        else:
            parts.append(_inline_raw(c.children, ctx))
    return "".join(parts)


def _code_language(pre: Node, code: Optional[Node]) -> str:
    for el in (code, pre):
        if el is None:
            continue
        for cls in sorted(el.classes):
            for prefix in ("language-", "lang-", "highlight-source-"):
                if cls.startswith(prefix):
                    return cls[len(prefix):]
        for attr in ("data-lang", "data-language"):
            if el.get(attr):
                return el.get(attr).strip().lower()
    parent = pre.parent
    if parent is not None:
        for cls in sorted(parent.classes):
            if cls.startswith("highlight-source-"):
                return cls[len("highlight-source-"):]
    return ""


def _render_pre(pre: Node) -> str:
    code = pre.find("code")
    target = code or pre
    text = "".join(x.text for x in target.iter() if x.is_text).strip("\n")
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{_code_language(pre, code)}\n{text}\n{fence}"


def _render_table(table: Node, ctx: _Ctx) -> Optional[str]:
    if any(x is not table and x.tag == "table" for x in table.iter()):
        return None
    matrix: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [c for c in tr.children if c.tag in ("td", "th")]
        row = [_collapse(_plain(c, " ")).replace("|", "\\|") for c in cells]
        if row:
            matrix.append(row)
    if not matrix:
        return None
    ncols = max(len(r) for r in matrix)
    if ncols < 2:
        return None
    for row in matrix:
        row.extend([""] * (ncols - len(row)))
    lines = ["| " + " | ".join(matrix[0]) + " |", "| " + " | ".join(["---"] * ncols) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in matrix[1:]]
    return "\n".join(lines)


def _render_list(lst: Node, ctx: _Ctx, level: int = 0) -> str:
    out: list[str] = []
    ordered = lst.tag == "ol"
    idx = 1
    for li in (c for c in lst.children if c.tag == "li"):
        text = _inline(li, ctx)
        indent = "  " * level
        if text:
            out.append(f"{indent}{idx}. {text}" if ordered else f"{indent}- {text}")
        if ordered:
            idx += 1
        for nested in (c for c in li.children if c.tag in ("ul", "ol")):
            md = _render_list(nested, ctx, level + 1)
            if md:
                out.append(md)
    return "\n".join(out)


def _walk(n: Node, blocks: list[str], ctx: _Ctx) -> None:
    buf: list[Node] = []

    def flush() -> None:
        if buf:
            text = _ws(_inline_raw(buf, ctx))
            if text:
                blocks.append(text)
            buf.clear()

    for c in n.children:
        if c.is_text:
            buf.append(c)
            continue
        if _md_skip(c):
            continue
        name = c.tag
        if _is_inline(c) and name not in ("br",):
            buf.append(c)
            continue
        if name == "br":
            buf.append(c)
            continue
        flush()
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _inline(c, ctx)
            if text:
                level = int(name[1])
                blocks.append("#" * level + " " + text)
                if len(ctx.headings) < _HEADINGS_CAP:
                    ctx.headings.append({"level": level, "text": text})
        elif name == "p":
            text = _inline(c, ctx)
            if text:
                blocks.append(text)
            for lst in (x for x in c.children if x.tag in ("ul", "ol")):
                md = _render_list(lst, ctx)
                if md:
                    blocks.append(md)
        elif name in ("ul", "ol"):
            md = _render_list(c, ctx)
            if md:
                blocks.append(md)
        elif name == "table":
            md = _render_table(c, ctx)
            if md is not None:
                blocks.append(md)
                ctx.tables += 1
            else:
                text = _collapse(_plain(c, " "))
                if text:
                    blocks.append(text)
        elif name == "pre":
            md = _render_pre(c)
            if md.strip("`\n "):
                blocks.append(md)
        elif name == "blockquote":
            inner: list[str] = []
            _walk(c, inner, ctx)
            body = "\n\n".join(b for b in inner if b.strip())
            if body:
                blocks.append("\n".join(f"> {line}" if line else ">" for line in body.splitlines()))
        elif name == "hr":
            blocks.append("---")
        else:
            _walk(c, blocks, ctx)
    flush()


def _select_main(doc: Node) -> Node:
    body = doc.find("body") or doc
    for sel in _MAIN_SELECTORS:
        found = [n for n in doc.iter() if n.tag != "#text" and _matches(n, sel)]
        if not found:
            continue
        best = max(found, key=_text_len)
        if _text_len(best) >= _THIN:
            return best
    areas = [n for n in doc.iter() if n.tag in ("main", "article", "section", "div") and _CONTENT_CLASS.search(n.get("class"))]
    if areas:
        ranked = sorted(areas, key=_text_len, reverse=True)
        chosen: list[Node] = []
        for area in ranked:
            if any(area in c.ancestors() or c in area.ancestors() for c in chosen):
                continue
            chosen.append(area)
            if len(chosen) == 3:
                break
        order = {id(n): i for i, n in enumerate(areas)}
        chosen.sort(key=lambda n: order[id(n)])
        wrapper = Node("div")
        wrapper.children = chosen
        return wrapper
    return body


def to_markdown(html: Any, base_url: str = "") -> dict[str, Any]:
    """The main content of an HTML document as GitHub-flavoured Markdown.

    Returns ``{"markdown", "title", "links", "headings", "tables"}``. ``links`` (``{text, url, internal}``, capped at
    200, de-duplicated) and ``headings`` (``{level, text}``, capped at 50) describe what the Markdown contains.
    Consecutive inline content inside a plain ``div`` becomes one paragraph (not one line per element)."""
    doc = parse_html(html)
    title = _title_of(doc)
    ctx = _Ctx(base_url)
    blocks: list[str] = []
    root = _select_main(doc)
    _walk(root, blocks, ctx)
    markdown = "\n\n".join(b for b in blocks if b.strip())
    body = doc.find("body") or doc
    if len(markdown) < _THIN and body is not root:
        alt_ctx = _Ctx(base_url)
        alt: list[str] = []
        _walk(body, alt, alt_ctx)
        alt_md = "\n\n".join(b for b in alt if b.strip())
        if len(alt_md) > len(markdown):
            markdown, ctx = alt_md, alt_ctx
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return {"markdown": markdown, "title": title, "links": ctx.links[:_LINKS_CAP], "headings": ctx.headings[:_HEADINGS_CAP],
            "tables": ctx.tables}


_FENCE_RE = re.compile(r"(`{3,})[^\n]*\n(.*?)\1", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.M)
_LIST_MARKER_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+", re.M)
_BLOCKQUOTE_RE = re.compile(r"^>\s?", re.M)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_HR_RE = re.compile(r"^-{3,}$", re.M)


def markdown_to_text(markdown: Any) -> str:
    """Strip Markdown syntax back to plain text (for sentence splitting and evidence matching)."""
    text = "" if markdown is None else str(markdown)
    text = _FENCE_RE.sub(lambda m: m.group(2), text)
    text = _HR_RE.sub("", text)
    text = _TABLE_SEP_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _LIST_MARKER_RE.sub(lambda m: m.group(1), text)
    text = _IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _BOLD_RE.sub(lambda m: m.group(1), text)
    text = _ITALIC_RE.sub(lambda m: m.group(1), text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and "|" in s[1:-1]:
            lines.append(" ".join(c.strip() for c in s.strip("|").split("|") if c.strip()))
        else:
            lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------------------------- quality and hashing
# Words that mostly occur in site chrome (menus, footers, account / cart links, social links).
NAV_WORDS = frozenset("""
inicio home menú menu categorías categorias buscar búsqueda busqueda login registro registrarse cuenta
carrito cesta pedidos favoritos wishlist ayuda contacto contáctanos contactanos envíos envios devoluciones
cookies privacidad aviso legal términos terminos condiciones política politica mapa sitio newsletter suscríbete
suscribete síguenos siguenos facebook instagram twitter youtube tiktok pinterest whatsapp idioma país pais
ofertas novedades outlet tiendas blog trabaja sobre nosotros empresa prensa afiliados sign account cart search
help shipping returns terms privacy careers stores
""".split())
_BLOCK_PHRASES = ("enable javascript", "captcha", "access denied", "verify you are human", "just a moment", "acceso denegado",
                  "activa javascript", "not a robot", "checking your browser", "unusual traffic")


def chrome_ratio(text: str) -> float:
    """Share of words that sit in short, menu-looking lines (<= 4 words with a navigation word): close to 0 for
    prose, close to 1 for a page that is only a menu or a footer."""
    total = chrome = 0
    for line in str(text or "").splitlines():
        words = re.findall(r"[^\W\d_]+", line.lower())
        if not words:
            continue
        total += len(words)
        if len(words) <= 4 and any(w in NAV_WORDS for w in words):
            chrome += len(words)
    return chrome / total if total else 1.0


def quality(text: Any, *, min_words: int = MIN_WORDS) -> str:
    """``""`` when ``text`` is a usable document, otherwise the reason it is not: ``"blocked page"`` (a short text
    that talks about CAPTCHAs, JavaScript or access), ``"too short"`` or ``"mostly navigation"``."""
    s = "" if text is None else str(text)
    words = s.split()
    if len(words) < 150 and any(p in s[:800].lower() for p in _BLOCK_PHRASES):
        return "blocked page"
    if len(words) < min_words:
        return "too short"
    lines = [l for l in s.splitlines() if l.strip()]
    short = sum(1 for l in lines if len(l.split()) <= 3)
    if lines and short / len(lines) > 0.85 and len(words) < 400:
        return "mostly navigation"
    if chrome_ratio(s) >= 0.6:
        return "mostly navigation"
    return ""


_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?(?!\d)", re.I)
_ISO_STAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?(?!\d)", re.I)
_UNIT = (r"(?:segundos?|segs?|minutos?|mins?|horas?|hrs?|h|d[ií]as?|semanas?|meses|"
         r"seconds?|secs?|minutes?|hours?|days?|weeks?|months?)")
_RELATIVE = re.compile(rf"\b(?:hace|quedan?|faltan?)\s+\d+\s+{_UNIT}\b|\b\d+\s+{_UNIT}\s+(?:ago|left|remaining)\b"
                       r"|\b(?:just now|justo ahora|ahora mismo|hace un momento)\b", re.I)
_TOKEN = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_VOLATILE_LINE = re.compile(r"^(?:©|\(c\)|copyright\b|hoy es\b|actualizado hace\b|updated\s+\d+\s+\w+\s+ago\b|"
                            r"last updated:?\s*(?:just now|\d+\s+\w+\s+ago)\b)", re.I)


def normalise_for_hash(text: Any) -> str:
    """Text with the volatile noise removed, one normalised line per line: NFKC and case folded; ISO timestamps,
    clock times, relative times ("hace 5 minutos", "Updated 3 mins ago", "quedan 3 horas") and long hex tokens
    (session and cache ids) removed; lines that are only a ``©``/copyright notice, "hoy es ..." or an "updated N ago"
    stamp dropped. Every other digit is kept on purpose: prices, quantities and dates are what a sentry must
    notice."""
    out: list[str] = []
    for raw in ("" if text is None else str(text)).splitlines():
        line = unicodedata.normalize("NFKC", raw).casefold()
        line = line.replace("​", "").replace("‌", "").replace("﻿", "")
        line = re.sub(r"\s+", " ", line).strip()
        if not line or _VOLATILE_LINE.match(line):
            continue
        for pattern in (_ISO_STAMP, _RELATIVE, _CLOCK, _TOKEN):
            line = pattern.sub(" ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def content_hash(text: Any) -> str:
    """SHA-256 (hex) of :func:`normalise_for_hash`."""
    return hashlib.sha256(normalise_for_hash(text).encode("utf-8")).hexdigest()


def excerpt(text: Any, max_chars: int = 300) -> str:
    """A short plain excerpt: whitespace collapsed, cut at a word boundary with an ellipsis."""
    return clamp_text(text, max_chars)
