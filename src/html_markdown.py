"""HTML -> structured Markdown conversion, in-repo (BeautifulSoup only, no
new dependency).

Why: a flat, whitespace-joined ``get_text()`` dump throws away everything
that makes a page's structure legible -- headings collapse into body text,
list items run together, tables turn into a word salad, and code samples
lose their fences. The model (and anything downstream that cites the page)
does better with the structure intact. This module renders a page's main
content region to GitHub-flavored Markdown instead: headings, ordered and
nested unordered lists, GFM pipe tables, fenced code blocks (with language
when the source markup carries one), inline code/emphasis, blockquotes, and
links resolved to absolute URLs.

Two entry points:
  * ``html_to_markdown(html, base_url)`` -> the markdown text plus
    structured ``links``/``headings``/``tables`` metadata.
  * ``markdown_to_text(markdown)`` -> the markdown stripped back to plain
    text, for callers that need to run text-oriented heuristics (sentence
    splitting for citations/evidence) on content that is now Markdown by
    default.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

# ---------------------------------------------------------------------------
# Boilerplate removal -- same spirit as services.search.content's stripping,
# reused here since the markdown renderer needs to walk the DOM itself
# rather than call get_text() on a pre-cleaned copy.
# ---------------------------------------------------------------------------
_NOISE_TAGS = (
    "script", "style", "noscript", "template", "nav", "header", "footer",
    "aside", "form", "iframe", "svg", "button", "select", "option",
    "textarea", "label", "input",
)

_CONTENT_CLASS_RE = re.compile(r"content|main|body|article|post|entry|text", re.I)

_THIN_CONTENT_CHARS = 600

_LINKS_CAP = 200
_HEADINGS_CAP = 50


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    # HTML comments carry no visible text but can otherwise leak into
    # NavigableString walks below.
    for comment in soup.find_all(string=lambda s: s.__class__.__name__ == "Comment"):
        comment.extract()


def _select_main(soup: BeautifulSoup) -> Tag:
    """Pick the node(s) to render, preferring semantic/"content"-classed
    containers (mirrors ``services.search.content``'s heuristic) and
    falling back to the whole (noise-stripped) body for thin pages."""
    body = soup.find("body") or soup

    content_areas = soup.find_all(
        ["main", "article", "section", "div"], class_=_CONTENT_CLASS_RE
    )
    if content_areas:
        wrapper = soup.new_tag("div")
        for area in content_areas[:3]:
            wrapper.append(area.extract())
        return wrapper
    return body


def _resolve_url(href: str, base_url: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    low = href.lower()
    if low.startswith(("javascript:", "data:", "vbscript:")):
        return None
    if low.startswith("mailto:") or low.startswith("tel:"):
        return href
    try:
        return urljoin(base_url, href) if base_url else href
    except Exception:
        return href or None


def _is_internal(url: str, base_url: str) -> bool:
    if not base_url:
        return False
    try:
        return (urlparse(url).netloc or "").lower() == (urlparse(base_url).netloc or "").lower()
    except Exception:
        return False


def _collapse_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------------------------------------------------------------------
# Inline rendering (text inside a paragraph/heading/table cell/list item)
# ---------------------------------------------------------------------------
def _inline(node, links: List[dict], headings_seen_urls, base_url: str) -> str:
    parts: List[str] = []
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in ("ul", "ol"):
            # Lists inside inline contexts (e.g. a <p> wrapping a <ul>) are
            # rendered by the block-level list renderer instead; skip here
            # so their items are not smeared into the surrounding sentence.
            continue
        if name in ("strong", "b"):
            inner = _inline(child, links, headings_seen_urls, base_url).strip()
            parts.append(f"**{inner}**" if inner else "")
        elif name in ("em", "i"):
            inner = _inline(child, links, headings_seen_urls, base_url).strip()
            parts.append(f"*{inner}*" if inner else "")
        elif name == "code":
            text = child.get_text().strip()
            parts.append(f"`{text}`" if text else "")
        elif name == "br":
            parts.append("\n")
        elif name == "a":
            href = child.get("href") or ""
            text = _inline(child, links, headings_seen_urls, base_url).strip() or href.strip()
            abs_url = _resolve_url(href, base_url)
            if not abs_url or not text:
                parts.append(text)
            else:
                if len(links) < _LINKS_CAP:
                    key = (text, abs_url)
                    if key not in headings_seen_urls:
                        headings_seen_urls.add(key)
                        links.append({
                            "text": text,
                            "url": abs_url,
                            "internal": _is_internal(abs_url, base_url),
                        })
                parts.append(f"[{text}]({abs_url})")
        elif name == "img":
            alt = (child.get("alt") or "").strip()
            src = (child.get("src") or "").strip()
            if alt and src:
                abs_src = _resolve_url(src, base_url) or src
                parts.append(f"![{alt}]({abs_src})")
        else:
            parts.append(_inline(child, links, headings_seen_urls, base_url))
    return _collapse_ws("".join(parts))


# ---------------------------------------------------------------------------
# Block-level rendering
# ---------------------------------------------------------------------------
def _render_pre(node: Tag) -> str:
    code_tag = node.find("code")
    lang = ""
    classes = (code_tag or node).get("class") or []
    for cls in classes:
        if cls.startswith("language-"):
            lang = cls[len("language-"):]
            break
        if cls.startswith("lang-"):
            lang = cls[len("lang-"):]
            break
    text = (code_tag or node).get_text()
    text = text.strip("\n")
    fence = "```"
    return f"{fence}{lang}\n{text}\n{fence}"


def _has_nested_table(node: Tag) -> bool:
    return node.find("table") is not None


def _render_table(node: Tag, links: List[dict], seen_links, base_url: str) -> Optional[str]:
    """GFM pipe table, or None when this table should be flattened to plain
    text instead (a nested table, or a single-column layout table)."""
    if _has_nested_table(node):
        return None

    matrix: List[List[str]] = []
    for tr in node.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        row = [_collapse_ws(c.get_text(" ", strip=True)).replace("|", "\\|") for c in cells]
        if row:
            matrix.append(row)
    if not matrix:
        return None

    ncols = max(len(r) for r in matrix)
    if ncols < 2:
        return None

    for row in matrix:
        while len(row) < ncols:
            row.append("")

    header, body_rows = matrix[0], matrix[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for row in body_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_list(node: Tag, links: List[dict], seen_links, base_url: str, level: int = 0) -> str:
    lines: List[str] = []
    idx = 1
    ordered = node.name.lower() == "ol"
    for li in node.find_all("li", recursive=False):
        nested = li.find_all(["ul", "ol"], recursive=False)
        text = _inline(li, links, seen_links, base_url)
        marker = f"{idx}." if ordered else "-"
        indent = "  " * level
        if text:
            lines.append(f"{indent}{marker} {text}")
        if ordered:
            idx += 1
        for nested_list in nested:
            nested_md = _render_list(nested_list, links, seen_links, base_url, level + 1)
            if nested_md:
                lines.append(nested_md)
    return "\n".join(lines)


def _walk_block(node, blocks: List[str], links: List[dict], headings: List[dict],
                 seen_links, table_count: List[int], base_url: str) -> None:
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            text = _collapse_ws(str(child))
            if text:
                blocks.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _inline(child, links, seen_links, base_url)
            if text:
                level = int(name[1])
                blocks.append(f"{'#' * level} {text}")
                if len(headings) < _HEADINGS_CAP:
                    headings.append({"level": level, "text": text})
        elif name == "p":
            text = _inline(child, links, seen_links, base_url)
            if text:
                blocks.append(text)
            # A <p> that wraps a <ul>/<ol> (invalid HTML but common) still
            # needs its list rendered -- _inline skips lists on purpose.
            for lst in child.find_all(["ul", "ol"], recursive=False):
                lst_md = _render_list(lst, links, seen_links, base_url)
                if lst_md:
                    blocks.append(lst_md)
        elif name in ("ul", "ol"):
            list_md = _render_list(child, links, seen_links, base_url)
            if list_md:
                blocks.append(list_md)
        elif name == "table":
            table_md = _render_table(child, links, seen_links, base_url)
            if table_md is not None:
                blocks.append(table_md)
                table_count[0] += 1
            else:
                text = _collapse_ws(child.get_text(" ", strip=True))
                if text:
                    blocks.append(text)
        elif name == "pre":
            code_md = _render_pre(child)
            if code_md.strip("`\n "):
                blocks.append(code_md)
        elif name == "blockquote":
            inner_blocks: List[str] = []
            _walk_block(child, inner_blocks, links, headings, seen_links, table_count, base_url)
            inner_md = "\n\n".join(b for b in inner_blocks if b.strip())
            if inner_md:
                quoted = "\n".join(f"> {line}" if line else ">" for line in inner_md.splitlines())
                blocks.append(quoted)
        elif name == "hr":
            blocks.append("---")
        elif name == "code":
            text = child.get_text().strip()
            if text:
                blocks.append(f"`{text}`")
        elif name == "img":
            alt = (child.get("alt") or "").strip()
            src = (child.get("src") or "").strip()
            if alt and src:
                abs_src = _resolve_url(src, base_url) or src
                blocks.append(f"![{alt}]({abs_src})")
        elif name == "a":
            # A bare block-level link with no wrapping <p> (occasional in
            # nav-stripped remnants of card-style markup).
            text = _inline(child, links, seen_links, base_url)
            if text:
                blocks.append(text)
        elif name == "br":
            continue
        else:
            # Generic container (div/section/article/span/figure/...): recurse.
            _walk_block(child, blocks, links, headings, seen_links, table_count, base_url)


def html_to_markdown(html: str, base_url: str = "") -> Dict:
    """Convert an HTML document's main content to Markdown.

    Returns ``{"markdown": str, "links": [...], "headings": [...], "tables": int}``.
    ``links`` and ``headings`` are capped (200 / 50) and links are deduped
    by (text, url).
    """
    html = html or ""
    soup = BeautifulSoup(html, "html.parser")
    _strip_noise(soup)

    links: List[dict] = []
    headings: List[dict] = []
    seen_links = set()
    table_count = [0]

    root = _select_main(soup)
    blocks: List[str] = []
    _walk_block(root, blocks, links, headings, seen_links, table_count, base_url)
    markdown = "\n\n".join(b for b in blocks if b.strip())

    # Thin-content fallback: the class-regex heuristic found only a tiny
    # wrapper (app shells, landing pages) -- fall back to the whole
    # (noise-stripped) body, same trigger threshold as the plain-text path.
    if len(markdown) < _THIN_CONTENT_CHARS:
        body = soup.find("body") or soup
        if body is not root:
            fallback_blocks: List[str] = []
            fallback_links: List[dict] = []
            fallback_headings: List[dict] = []
            fallback_seen = set()
            fallback_tables = [0]
            _walk_block(body, fallback_blocks, fallback_links, fallback_headings,
                        fallback_seen, fallback_tables, base_url)
            fallback_markdown = "\n\n".join(b for b in fallback_blocks if b.strip())
            if len(fallback_markdown) > len(markdown):
                markdown = fallback_markdown
                links = fallback_links
                headings = fallback_headings
                table_count = fallback_tables

    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return {
        "markdown": markdown,
        "links": links[:_LINKS_CAP],
        "headings": headings[:_HEADINGS_CAP],
        "tables": table_count[0],
    }


# ---------------------------------------------------------------------------
# markdown_to_text -- strip Markdown syntax back to plain text for callers
# that need it (sentence splitting for citations/evidence matching).
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_LIST_MARKER_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.MULTILINE)
_HR_RE = re.compile(r"^-{3,}$", re.MULTILINE)


def markdown_to_text(markdown: str) -> str:
    """Strip Markdown syntax back to plain text, for callers that need to
    run text-oriented heuristics (sentence splitting for citations/evidence)
    on content that is Markdown by default."""
    text = markdown or ""

    # Pull code fence bodies out first (their content is real text, but must
    # not have the fence markers or any other rule applied inside it).
    def _keep_fence_body(m):
        return m.group(1)
    text = _FENCE_RE.sub(_keep_fence_body, text)

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

    # GFM pipe-table rows: drop leading/trailing pipes, turn " | " into a
    # plain separator between cell text.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            lines.append(" ".join(c for c in cells if c))
        else:
            lines.append(line)
    text = "\n".join(lines)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
