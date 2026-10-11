"""Split text into overlapping chunks that keep where they came from (page, section, line, offsets).

Standard library only. ``chunk_text`` has a Node twin in ``js/hoard-commons/docs.js`` (``chunkText``) and both
cut at the same places (``tests/vectors/docs_chunks.json``).

The rules are Borges' (``borges/chunking.py``, index version 3):

* a chunk is about ``size`` characters and ends at the best break in its window: paragraph, line, sentence,
  clause, word, in that order of preference, never in the first 40 % of the window;
* the next chunk starts ``overlap`` characters back, moved to a word start;
* a tail shorter than ``min_tail`` is absorbed by the previous chunk;
* a page or section shorter than ``min_unit`` is merged into its neighbour, **except** pages: a physical page
  number is a citation coordinate, so pages are never merged;
* a chunk shorter than ``min_chunk`` is glued to the next one of the same unit.

``chunk_markdown`` adds Vitruvius' heading stack (``Guide § Install § Windows``) and front matter handling, and
splits oversized paragraphs at sentence/word breaks instead of hard-cutting them.

Bump :data:`CHUNK_VERSION` whenever a boundary rule changes so apps can re-index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

__all__ = ["CHUNK_VERSION", "Chunk", "Unit", "chunk_text", "chunk_units", "chunk_markdown", "merge_small_units",
           "markdown_title", "parse_frontmatter"]

CHUNK_VERSION = 4
CHUNK_CHARS = 900          # ~220 Spanish tokens: inside a small embedding model's useful window
OVERLAP_CHARS = 150
MIN_TAIL = 200
MIN_UNIT_CHARS = 200
MIN_CHUNK_CHARS = 120

_BREAKS = re.compile(r"\n\n|\n|(?<=[.!?…;:])\s+|(?<=,)\s+|\s+")


@dataclass
class Unit:
    """A page, section or chapter of a document (what an extractor produces). Dicts with the same keys work too."""
    kind: str = "section"          # page | section | chapter | slide | sheet | text
    number: int = 1                # page number (1-based) or ordinal
    title: str = ""
    text: str = ""
    line_start: Optional[int] = None   # 1-based line in the source file, for text formats


@dataclass
class Chunk:
    unit_index: int                # position in the unit list
    ordinal: int                   # position within the whole document
    page: Optional[int]            # physical page/slide number, else None
    section: str                   # the unit title (heading breadcrumb for markdown)
    line: Optional[int]            # 1-based source line of the chunk start (when the unit knows its first line)
    char_start: int                # offsets inside the unit text
    char_end: int
    text: str


def _as_unit(u: Any) -> Unit:
    if isinstance(u, Unit):
        return u
    get = u.get if hasattr(u, "get") else (lambda k, d=None: getattr(u, k, d))
    return Unit(kind=str(get("kind", "section") or "section"), number=int(get("number", 1) or 1),
                title=str(get("title", "") or ""), text=str(get("text", "") or ""), line_start=get("line_start", None))


def _split_point(text: str, start: int, limit: int) -> int:
    """Best cut position in text[start:limit]: paragraph > line > sentence > clause > word."""
    window = text[start:limit]
    best = -1
    best_priority = -1
    for match in _BREAKS.finditer(window):
        if match.end() < len(window) * 0.4:      # don't cut too early
            continue
        token = match.group(0)
        before = window[match.start() - 1:match.start()] if match.start() > 0 else ""
        if token == "\n\n":
            score = 5
        elif token == "\n":
            score = 4
        elif before and before in ".!?…;:":
            score = 3
        elif before == ",":
            score = 2
        else:
            score = 1
        if score >= best_priority:
            best_priority = score
            best = match.end()
    return start + best if best > 0 else limit


def merge_small_units(units: list[Unit], minimum: int = MIN_UNIT_CHARS) -> list[Unit]:
    """Merge units shorter than ``minimum`` into the following unit (the previous one at the end). The merged unit
    keeps the metadata of the larger part; the short ones' titles ride along as heading lines. Pages are never
    merged: a document with any physical ``page`` or ``slide`` unit is returned as it is."""
    if len(units) <= 1 or any(u.kind in ("page", "slide") for u in units):
        return list(units)
    pending: list[Unit] = []
    out: list[Unit] = []
    for unit in units:
        if len(unit.text) < minimum:
            pending.append(unit)
            continue
        if pending:
            unit = _absorb(unit, pending, before=True)
            pending = []
        out.append(unit)
    if pending:
        if out:
            out[-1] = _absorb(out[-1], pending, before=False)
        else:                                     # every unit is short: keep the longest one's metadata
            biggest = max(pending, key=lambda u: len(u.text))
            rest = [u for u in pending if u is not biggest]
            out.append(_absorb(biggest, rest, before=True))
    return out


def _absorb(main: Unit, others: list[Unit], before: bool) -> Unit:
    parts = [(f"{u.title}\n{u.text}" if u.title else u.text) for u in others]
    text = "\n\n".join([*parts, main.text] if before else [main.text, *parts])
    return Unit(kind=main.kind, number=main.number, title=main.title, text=text, line_start=main.line_start)


def _chunk_unit(unit: Unit, unit_index: int, first_ordinal: int, size: int, overlap: int, min_tail: int) -> list[Chunk]:
    text = unit.text
    page = unit.number if unit.kind in ("page", "slide") else None
    chunks: list[Chunk] = []
    start = 0
    n = len(text)
    while start < n:
        end = n if n - start <= size + min_tail else _split_point(text, start, start + size)
        piece = text[start:end]
        stripped = piece.strip()
        if stripped:
            lead = len(piece) - len(piece.lstrip())
            c_start, c_end = start + lead, start + lead + len(stripped)
            line = unit.line_start + text.count("\n", 0, c_start) if unit.line_start is not None else None
            chunks.append(Chunk(unit_index, first_ordinal + len(chunks), page, unit.title, line, c_start, c_end, stripped))
        if end >= n:
            break
        next_start = max(end - overlap, start + 1)
        boundary = text.find(" ", max(0, end - overlap), end)      # move the overlap start to a word boundary
        if boundary > start:
            next_start = boundary + 1
        start = next_start
    return chunks


def _absorb_tiny(units: list[Unit], chunks: list[Chunk], min_chunk: int) -> list[Chunk]:
    if len(chunks) <= 1:
        return chunks
    result: list[Chunk] = []
    for chunk in chunks:
        if len(chunk.text) >= min_chunk or not result or result[-1].unit_index != chunk.unit_index:
            result.append(chunk)
            continue
        previous = result[-1]
        previous.char_end = chunk.char_end
        previous.text = units[previous.unit_index].text[previous.char_start:previous.char_end].strip()
    cleaned: list[Chunk] = []
    for index, chunk in enumerate(result):
        nxt = result[index + 1] if index + 1 < len(result) else None
        if len(chunk.text) < min_chunk and nxt is not None and nxt.unit_index == chunk.unit_index:
            nxt.char_start = chunk.char_start
            nxt.text = units[nxt.unit_index].text[nxt.char_start:nxt.char_end].strip()
            nxt.line = chunk.line
            continue
        cleaned.append(chunk)
    for ordinal, chunk in enumerate(cleaned):
        chunk.ordinal = ordinal
    return cleaned


def _clamp(size: int, overlap: int, min_chunk: int) -> tuple[int, int, int]:
    size = max(1, int(size))
    return size, max(0, min(int(overlap), size // 2)), max(0, min(int(min_chunk), size // 2))


def chunk_units(units: Iterable[Any], *, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS,
                min_unit: int = MIN_UNIT_CHARS, min_chunk: int = MIN_CHUNK_CHARS, min_tail: int = MIN_TAIL) -> list[Chunk]:
    """Chunk a document given as units (``Unit`` objects or dicts with ``kind``/``number``/``title``/``text``/
    ``line_start``). Short non-page units are merged first, chunks never cross a unit boundary, and ``page`` /
    ``section`` / ``line`` ride on every chunk. ``overlap`` and ``min_chunk`` are limited to half the size, so small sizes
    still make progress (an overlap close to the size would advance a few characters per chunk)."""
    size, overlap, min_chunk = _clamp(size, overlap, min_chunk)
    prepared = merge_small_units([_as_unit(u) for u in units], min_unit)
    out: list[Chunk] = []
    for index, unit in enumerate(prepared):
        out.extend(_chunk_unit(unit, index, len(out), size, overlap, min_tail))
    return _absorb_tiny(prepared, out, min_chunk)


def chunk_text(text: Any, *, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS, min_tail: int = MIN_TAIL) -> list[Chunk]:
    """Chunk plain text (one unit; ``line`` counts from 1). Offsets are into ``text``."""
    s = "" if text is None else str(text)
    return chunk_units([Unit("text", 1, "", s, 1)], size=size, overlap=overlap, min_unit=0, min_tail=min_tail)


# ---- markdown --------------------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def parse_frontmatter(text: str) -> tuple[dict[str, str], int]:
    """``({key: value}, body_offset)`` for a leading ``---`` YAML block (flat ``key: value`` lines only; keys lowercase)."""
    m = _FRONTMATTER.match(text or "")
    if not m:
        return {}, 0
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, m.end()


def _sections(body: str, line0: int) -> list[tuple[list[str], str, int]]:
    """(heading stack, content, 1-based line of the content's first line), skipping headings inside code fences."""
    sections: list[tuple[list[str], str, int]] = []
    stack: list[str] = []
    current: list[str] = []
    first_line = line0 + 1
    fence = ""

    def flush() -> None:
        content = "\n".join(current)
        if content.strip():
            lead = len(content) - len(content.lstrip("\n"))
            sections.append((list(stack), content.strip(), first_line + content[:lead].count("\n")))

    for offset, line in enumerate(body.split("\n")):
        line = line.rstrip("\r")
        fm = _FENCE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
        m = None if fence or fm else _HEADING.match(line)
        if m:
            flush()
            current = []
            first_line = line0 + offset + 2
            level = len(m.group(1))
            stack = stack[:level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(m.group(2).strip())
        else:
            current.append(line)
    flush()
    return sections


def markdown_title(text: str, default: str = "", *, frontmatter: bool = True) -> str:
    """The front matter ``title:`` (when ``frontmatter``), else the first ``# Heading``, else ``default``."""
    s = text or ""
    offset = 0
    if frontmatter:
        meta, offset = parse_frontmatter(s)
        if meta.get("title"):
            return meta["title"]
    fence = ""
    for line in s[offset:].split("\n"):
        line = line.rstrip("\r")
        fm = _FENCE.match(line)
        if fm:
            fence = "" if fence == fm.group(1) else (fence or fm.group(1))
            continue
        m = None if fence else re.match(r"^#[ \t]+(.*?)[ \t#]*$", line)
        if m:
            return m.group(1).strip()
    return default


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in re.finditer(r"\n[ \t]*\n", text):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans


def _pack_section(text: str, size: int) -> list[tuple[int, int]]:
    """Offsets of pieces of at most ``size`` characters: whole paragraphs packed greedily, an oversized paragraph
    cut at sentence or word breaks."""
    if len(text) <= size:
        return [(0, len(text))] if text.strip() else []
    pieces: list[tuple[int, int]] = []
    cur: Optional[list[int]] = None
    for a, z in _paragraph_spans(text):
        while z - a > size:                       # oversized paragraph: Borges-style cut
            cut = _split_point(text, a, a + size)
            if cut <= a:
                cut = a + size
            if cur is not None:
                pieces.append((cur[0], cur[1]))
                cur = None
            pieces.append((a, cut))
            a = cut
            while a < z and text[a].isspace():
                a += 1
        if a >= z:
            continue
        if cur is not None and z - cur[0] > size:
            pieces.append((cur[0], cur[1]))
            cur = None
        if cur is None:
            cur = [a, z]
        else:
            cur[1] = z
    if cur is not None:
        pieces.append((cur[0], cur[1]))
    return pieces


def chunk_markdown(text: Any, *, size: int = 1200, breadcrumbs: bool = True, frontmatter: bool = True,
                   default_title: str = "") -> list[Chunk]:
    """Chunk a markdown document by heading. Each chunk is at most ``size`` characters of one section (paragraphs
    packed together, an oversized paragraph cut at sentence/word breaks). ``section`` is the heading breadcrumb
    ``"Guide § Install § Windows"`` (``breadcrumbs=True``) or just the innermost heading; text before the first
    heading gets the document title (:func:`markdown_title`, or ``default_title``). ``frontmatter=True`` drops a
    leading ``---`` block. Headings inside code fences are not headings. ``unit_index`` is the section number,
    offsets are inside the section text, ``line`` is the 1-based source line."""
    s = "" if text is None else str(text)
    offset = parse_frontmatter(s)[1] if frontmatter else 0
    line0 = s[:offset].count("\n")
    title = markdown_title(s, default_title, frontmatter=frontmatter)
    sections = _sections(s[offset:], line0)
    if not sections and s[offset:].strip():
        sections = [([], s[offset:].strip(), line0 + 1)]
    chunks: list[Chunk] = []
    for index, (stack, content, first_line) in enumerate(sections):
        heads = [h for h in stack if h]
        label = (" § ".join(heads) if breadcrumbs else (heads[-1] if heads else "")) or title
        for a, z in _pack_section(content, max(1, int(size))):
            piece = content[a:z]
            lead = len(piece) - len(piece.lstrip())
            body = piece.strip()
            if not body:
                continue
            c_start = a + lead
            chunks.append(Chunk(index, len(chunks), None, label, first_line + content.count("\n", 0, c_start),
                                c_start, c_start + len(body), body))
    return chunks
