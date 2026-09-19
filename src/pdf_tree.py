"""src/pdf_tree.py — deterministic table-of-contents navigation for long PDFs.

Idea (from a "tree index" vectorless-RAG note): instead of chunking a PDF and
retrieving by embedding similarity, build a structural tree of the document
(outline/bookmarks -> heading detection -> fixed page chunks, in that
preference order) with an EXACT physical page range per node, then let the
model read exactly the section it needs. Page numbers in every result come
from this module's own parse of the PDF, never from the model, so a model
can never invent "page 42" — it can only ask for a node id this module
handed it.

Path confinement: mirrors `src.pdf_ops.resolve_path` exactly (same
underlying guard, `src.tool_execution._resolve_tool_path` — the active
workspace / `DATA_DIR` allowlist), so this module never invents a second
notion of "safe path".

Dependencies: `pypdf` is a hard dependency (`requirements.txt`) and does all
the real work here (outline destinations, text extraction). `pdfplumber`
(optional — see `requirements-optional.txt`) is used, when installed, only
to sharpen heading detection with font-size information; every code path
here works correctly without it, just with a slightly less precise
`"headings"` fallback.

Caching: `build_tree` is the expensive call (walks the outline or scans
every page for headings), so results are cached in-process in a bounded LRU
keyed by `(resolved_path, mtime, size)` — nothing is written to disk, and a
file that changes on disk (mtime/size differ) simply misses the cache and
gets rebuilt, so the cache can never serve a stale tree.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PdfTreeError(ValueError):
    """A request this module refuses to carry out, with the reason a human
    (or the model reading the tool result) needs."""


# ---------------------------------------------------------------------------
# Path confinement — same guard as src.pdf_ops.resolve_path.
# ---------------------------------------------------------------------------

def resolve_path(raw_path: str) -> str:
    """Confine `raw_path` to the active workspace / DATA_DIR allowlist.

    Delegates to `src.tool_execution._resolve_tool_path`, the SAME function
    `src.pdf_ops.resolve_path` calls — never a second implementation of the
    guard.
    """
    from src.tool_execution import _resolve_tool_path
    try:
        return _resolve_tool_path(raw_path)
    except ValueError as exc:
        raise PdfTreeError(str(exc)) from exc


def _require_pypdf():
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover — pypdf is a hard dependency
        raise PdfTreeError(
            "pypdf is required for PDF navigation and should already be installed "
            "(see requirements.txt); reinstall with `pip install pypdf`"
        ) from exc
    return pypdf


def _open_reader(path: str):
    pypdf = _require_pypdf()
    try:
        return pypdf.PdfReader(path)
    except Exception as exc:  # noqa: BLE001 — a corrupt/encrypted PDF is data, not a crash
        raise PdfTreeError(f"could not open '{path}' as a PDF: {exc}") from exc


# ---------------------------------------------------------------------------
# In-process LRU cache: (resolved_path, mtime, size) -> tree dict.
# ---------------------------------------------------------------------------

_TREE_CACHE_MAX = 32
_tree_cache: "OrderedDict[Tuple[str, float, int], Dict[str, Any]]" = OrderedDict()


def _cache_store(key: Tuple[str, float, int], tree: Dict[str, Any]) -> None:
    _tree_cache.pop(key, None)
    _tree_cache[key] = tree
    while len(_tree_cache) > _TREE_CACHE_MAX:
        _tree_cache.popitem(last=False)  # oldest first


def clear_cache() -> None:
    """Test hook — drop every cached tree."""
    _tree_cache.clear()


def _get_tree(resolved_path: str) -> Dict[str, Any]:
    try:
        st = os.stat(resolved_path)
    except OSError as exc:
        raise PdfTreeError(f"could not read '{resolved_path}': {exc}") from exc
    key = (resolved_path, st.st_mtime, st.st_size)
    cached = _tree_cache.get(key)
    if cached is not None:
        _tree_cache.move_to_end(key)
        return cached
    tree = _build_tree_uncached(resolved_path)
    _cache_store(key, tree)
    return tree


# ---------------------------------------------------------------------------
# Tree construction — outline -> headings -> fixed page chunks.
# ---------------------------------------------------------------------------

def _build_tree_uncached(resolved_path: str) -> Dict[str, Any]:
    reader = _open_reader(resolved_path)
    total_pages = len(reader.pages)
    if total_pages <= 0:
        raise PdfTreeError("document has no pages")

    nested = _try_outline(reader)
    source = "outline"
    if not nested:
        nested = _try_headings(resolved_path, reader, total_pages)
        source = "headings"
    if not nested:
        nested = _page_chunks(total_pages)
        source = "pages"

    _assign_ids(nested)
    _assign_end_pages(nested, total_pages)
    return {"pages": total_pages, "source": source, "nodes": nested}


def _walk_outline(items: List[Any], reader: Any, level: int) -> List[Dict[str, Any]]:
    """Walk pypdf's `reader.outline` shape: a list whose items are either an
    outline entry (has `.title`, resolvable to a page via
    `get_destination_page_number`) or a nested `list` — the children of the
    entry immediately before it. Unresolvable entries are skipped rather
    than guessed at."""
    nodes: List[Dict[str, Any]] = []
    i = 0
    n = len(items)
    while i < n:
        item = items[i]
        if isinstance(item, list):
            # A list with no preceding entry is malformed input — ignore it.
            i += 1
            continue
        i += 1
        try:
            page0 = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001 — an unresolvable bookmark is skipped
            page0 = None
        children: List[Dict[str, Any]] = []
        if i < n and isinstance(items[i], list):
            children = _walk_outline(items[i], reader, level + 1)
            i += 1
        if page0 is None:
            # Keep resolvable children even if this entry itself is broken.
            nodes.extend(children)
            continue
        title = str(getattr(item, "title", "") or "").strip() or "Untitled"
        nodes.append({"title": title, "level": level, "start_page": page0 + 1, "children": children})
    return nodes


def _try_outline(reader: Any) -> List[Dict[str, Any]]:
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001 — malformed /Outlines dict
        return []
    if not outline:
        return []
    try:
        nested = _walk_outline(outline, reader, 1)
    except Exception:  # noqa: BLE001 — never let a weird bookmark tree crash this
        return []
    return nested


# --- heading detection (fallback when there is no usable outline) --------

# Conservative on purpose — a false heading is worse than none. Matches
# "1", "1.2", "1.2.3." style numbered sections, "Chapter"/"Capítulo",
# "Section"/"Sección", "Anexo"/"Appendix", and a roman-numeral heading like
# "IV. Scope".
_NUM_HEADING_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,4})\.?\s+\S")
_KEYWORD_HEADING_RE = re.compile(
    r"^(chapter|cap[ií]tulo|section|secci[oó]n|anexo|appendix)\s+\S", re.IGNORECASE
)
_ROMAN_HEADING_RE = re.compile(r"^([IVXLCDM]{1,7})\.\s+\S")
_MAX_HEADING_LEN = 120


def _detect_heading_level(line: str) -> Optional[int]:
    if not line or len(line) > _MAX_HEADING_LEN:
        return None
    m = _NUM_HEADING_RE.match(line)
    if m:
        depth = m.group(1).count(".") + 1
        return min(depth, 6)
    m = _KEYWORD_HEADING_RE.match(line)
    if m:
        return 2 if m.group(1).lower().startswith(("section", "secci")) else 1
    if _ROMAN_HEADING_RE.match(line):
        return 1
    return None


def _lines_from_pypdf(reader: Any, page_index: int) -> List[str]:
    try:
        text = reader.pages[page_index].extract_text() or ""
    except Exception:  # noqa: BLE001 — a broken page's text is just absent
        text = ""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _font_based_headings(page: Any) -> set:
    """Best-effort: lines whose max font size is clearly larger than the
    page's most common (body) font size — only used when pdfplumber is
    installed. Returns a set of heading TEXT (line-level, not page-level)."""
    try:
        chars = page.chars
    except Exception:  # noqa: BLE001
        return set()
    if not chars:
        return set()
    sizes = Counter(round(c.get("size", 0), 1) for c in chars if c.get("size"))
    if not sizes:
        return set()
    body_size = sizes.most_common(1)[0][0]
    if not body_size:
        return set()
    lines: Dict[float, List[Any]] = {}
    for c in chars:
        key = round(c.get("top", 0), 0)
        lines.setdefault(key, []).append(c)
    headings = set()
    for cs in lines.values():
        try:
            text = "".join(c.get("text", "") for c in sorted(cs, key=lambda c: c.get("x0", 0))).strip()
        except Exception:  # noqa: BLE001
            continue
        if not text or len(text) > _MAX_HEADING_LEN:
            continue
        max_size = max((c.get("size", 0) for c in cs), default=0)
        if max_size >= body_size * 1.15:
            headings.add(text)
    return headings


def _try_headings(resolved_path: str, reader: Any, total_pages: int) -> List[Dict[str, Any]]:
    headings: List[Dict[str, Any]] = []  # {"title", "level", "page"} in reading order

    plumber = None
    try:
        import pdfplumber  # optional dependency — see requirements-optional.txt
        plumber = pdfplumber.open(resolved_path)
    except Exception:  # noqa: BLE001 — not installed, or this PDF trips it up
        plumber = None

    try:
        for page_index in range(total_pages):
            page_num = page_index + 1
            lines = _lines_from_pypdf(reader, page_index)
            font_headings: set = set()
            if plumber is not None:
                try:
                    font_headings = _font_based_headings(plumber.pages[page_index])
                except Exception:  # noqa: BLE001
                    font_headings = set()
            for line in lines:
                level = _detect_heading_level(line)
                if level is None and line not in font_headings:
                    continue
                headings.append({"title": line, "level": level or 1, "page": page_num})
    finally:
        if plumber is not None:
            try:
                plumber.close()
            except Exception:  # noqa: BLE001
                pass

    if not headings:
        return []
    return _nest_by_level(headings)


def _nest_by_level(headings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn a flat, reading-order list of {"title","level","page"} into a
    nested tree the same shape `_walk_outline` produces — each heading
    becomes a child of the most recently seen heading at a shallower level."""
    root: List[Dict[str, Any]] = []
    stack: List[Tuple[int, Dict[str, Any]]] = []  # (level, node)
    for h in headings:
        node = {"title": h["title"], "level": h["level"], "start_page": h["page"], "children": []}
        while stack and stack[-1][0] >= node["level"]:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            root.append(node)
        stack.append((node["level"], node))
    return root


def _page_chunks(total_pages: int, chunk_size: int = 10) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    start = 1
    while start <= total_pages:
        end = min(start + chunk_size - 1, total_pages)
        nodes.append({"title": f"pages {start}-{end}", "level": 1, "start_page": start, "children": []})
        start = end + 1
    return nodes


# --- id assignment + end_page computation (shared by every source) -------

def _assign_ids(nodes: List[Dict[str, Any]], prefix: str = "") -> None:
    counter = 0
    for node in nodes:
        counter += 1
        node["id"] = str(counter) if not prefix else f"{prefix}.{counter}"
        if node["children"]:
            _assign_ids(node["children"], node["id"])


def _flatten_preorder(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(ns: List[Dict[str, Any]]) -> None:
        for n in ns:
            out.append(n)
            walk(n["children"])

    walk(nodes)
    return out


def _assign_end_pages(nodes: List[Dict[str, Any]], total_pages: int) -> None:
    """end_page = (next sibling/ancestor-sibling)'s start_page - 1, i.e. the
    next node in pre-order whose level is <= this node's own level (a
    deeper node is a descendant and stays inside this node's range); the
    last such node in the document runs to the last page."""
    flat = _flatten_preorder(nodes)
    n = len(flat)
    for i, node in enumerate(flat):
        end = total_pages
        for j in range(i + 1, n):
            if flat[j]["level"] <= node["level"]:
                end = flat[j]["start_page"] - 1
                break
        node["end_page"] = max(end, node["start_page"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_tree(path: str) -> Dict[str, Any]:
    """{"pages": N, "source": "outline"|"headings"|"pages", "nodes": [...]}.

    Each node: {id, title, level, start_page, end_page, children}. Page
    numbers are 1-based physical pages. Cached (see module docstring)."""
    resolved = resolve_path(path)
    return _get_tree(resolved)


def _find_node(tree: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    for node in _flatten_preorder(tree["nodes"]):
        if node["id"] == node_id:
            return node
    return None


def read_section(path: str, node_id: str, *, max_chars: int = 20000) -> Dict[str, Any]:
    """Text of `node_id`'s page range, one "[page N]" marker per page,
    clipped to `max_chars`. Page numbers only ever come from the tree —
    never from the caller/model — so `node_id` is validated against it."""
    if max_chars <= 0:
        raise PdfTreeError("max_chars must be positive")
    resolved = resolve_path(path)
    tree = _get_tree(resolved)
    node_id = str(node_id or "").strip()
    if not node_id:
        raise PdfTreeError("`node_id` is required — call pdf_outline first to get one")
    node = _find_node(tree, node_id)
    if node is None:
        raise PdfTreeError(
            f"no section with id '{node_id}' in this document's tree — call pdf_outline "
            f"(or pdf_find_section) again to get a current, valid node id"
        )

    reader = _open_reader(resolved)
    parts: List[str] = []
    total_chars = 0
    stopped_at_page: Optional[int] = None
    for page_num in range(node["start_page"], node["end_page"] + 1):
        try:
            page_text = reader.pages[page_num - 1].extract_text() or ""
        except Exception:  # noqa: BLE001 — a broken page's text is just absent
            page_text = ""
        chunk = f"[page {page_num}]\n{page_text}".rstrip("\n")
        addition = ("\n\n" if parts else "") + chunk
        if total_chars + len(addition) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 0:
                parts.append(addition[:remaining])
            stopped_at_page = page_num
            break
        parts.append(addition)
        total_chars += len(addition)

    result: Dict[str, Any] = {
        "id": node["id"],
        "title": node["title"],
        "start_page": node["start_page"],
        "end_page": node["end_page"],
        "text": "".join(parts),
        "truncated": stopped_at_page is not None,
    }
    if stopped_at_page is not None:
        result["note"] = (
            f"Output clipped at max_chars ({max_chars}); stopped partway through "
            f"page {stopped_at_page} of the section's range "
            f"({node['start_page']}-{node['end_page']}). Raise max_chars or read the "
            f"remaining pages as a follow-up call."
        )
    return result


def format_outline_text(tree: Dict[str, Any], max_depth: Optional[int] = None) -> str:
    """Compact indented tree: "id  title  (pp. a-b)" per line, one per node,
    plus a one-line header naming the page count and how the tree was
    built."""
    lines: List[str] = []

    def walk(nodes: List[Dict[str, Any]], depth: int) -> None:
        if max_depth is not None and depth > max_depth:
            return
        for node in nodes:
            indent = "  " * (depth - 1)
            lines.append(
                f"{indent}{node['id']}  {node['title']}  (pp. {node['start_page']}-{node['end_page']})"
            )
            walk(node["children"], depth + 1)

    walk(tree["nodes"], 1)
    header = f"{tree['pages']} pages, tree source: {tree['source']}"
    return header + ("\n" + "\n".join(lines) if lines else "")


def find_in_tree(path: str, query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Cheap title/keyword match over node titles (and, when `source ==
    "pages"`, over each node's own page text) to help the model pick a
    node id before calling `read_section`. Returns node id/title/pages,
    best match first."""
    query = str(query or "").strip()
    if not query:
        raise PdfTreeError("`query` is required")
    resolved = resolve_path(path)
    tree = _get_tree(resolved)
    flat = _flatten_preorder(tree["nodes"])

    q_lower = query.lower()
    q_tokens = set(q_lower.split())
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for node in flat:
        title_lower = node["title"].lower()
        if q_lower in title_lower:
            scored.append((10, node))
            continue
        overlap = len(q_tokens & set(title_lower.split()))
        if overlap:
            scored.append((overlap, node))

    if not scored and tree["source"] == "pages":
        reader = _open_reader(resolved)
        for node in flat:
            found = False
            for page_num in range(node["start_page"], node["end_page"] + 1):
                try:
                    text = (reader.pages[page_num - 1].extract_text() or "").lower()
                except Exception:  # noqa: BLE001
                    text = ""
                if q_lower in text:
                    found = True
                    break
            if found:
                scored.append((1, node))

    scored.sort(key=lambda pair: -pair[0])
    out: List[Dict[str, Any]] = []
    seen = set()
    for _, node in scored:
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        out.append({
            "id": node["id"],
            "title": node["title"],
            "start_page": node["start_page"],
            "end_page": node["end_page"],
        })
        if len(out) >= limit:
            break
    return out
