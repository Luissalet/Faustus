"""Clean text that comes out of PDFs, Word files and the web, and decode bytes of unknown encoding.

Standard library only.

* :func:`clean_text` — invisible characters, hyphenation across lines, whitespace, stacked words.
* :func:`unstack_words` — some PDF exporters put every word on its own line; join them back.
* :func:`strip_repeated_lines` — drop page headers and footers (lines on more than half of the pages).
* :func:`useful_chars` — letters and digits, to tell a page with a text layer from a scan.
* :func:`decode_text` — UTF-8 (with or without BOM), UTF-16/32 with BOM, then Windows-1252, then Latin-1.
* :func:`split_pages` — cut a long text into page-sized pieces on paragraph and line breaks.

Replaces: Hypatia ``notebook/sources.py`` (``_clean``, ``_unstack_words``, ``strip_repeated_lines``), Kafka
``readers.py`` (``clean_text``, ``useful_chars``, ``decode_text``, ``split_pages``), Cicero ``extract.decode_text`` /
``normalize_text``, Borges ``extract/base.clean_text`` / ``read_text``, Pygmalion ``_read_text_file``.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

__all__ = ["clean_text", "unstack_words", "strip_repeated_lines", "useful_chars", "decode_text", "split_pages"]

_INVISIBLE = dict.fromkeys(map(ord, "\x00­​‌‍⁠﻿"), None)
_SPACES = re.compile(r"[ \t  -   　]+")


def clean_text(text: object, *, dehyphenate: bool = True, unstack: bool = True, max_chars: Optional[int] = None) -> str:
    """Tidy extracted text: ``\\r\\n`` -> ``\\n``; NUL, soft hyphens, zero-width characters and BOMs removed;
    non-breaking and exotic spaces -> a space; runs of spaces collapsed; spaces around line breaks trimmed; three
    or more blank lines -> one blank line; ``word-\\nrest`` re-joined when the next line starts lowercase
    (``dehyphenate``); one-word-per-line text re-joined (``unstack``). ``max_chars`` cuts the result."""
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE)
    s = _SPACES.sub(" ", s)
    if dehyphenate:
        s = re.sub(r"([^\W\d_])-\n([^\W\d_])", lambda m: m.group(1) + m.group(2) if m.group(2).islower() else m.group(0), s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if unstack:
        s = unstack_words(s)
    return s[:max_chars] if max_chars else s


def unstack_words(text: str) -> str:
    """Some PDF exporters put every word on its own line (``menor\\n\\nuso\\n\\ndirecto``): join them back into running
    text, or each chunk wastes most of its size on breaks. Only applies when 20+ lines, more than 60 % of them
    one word and the median line at most 14 characters."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < 20:
        return text
    single = sum(1 for ln in lines if " " not in ln.strip())
    lengths = sorted(len(ln) for ln in lines)
    if single / len(lines) > 0.6 and lengths[len(lengths) // 2] <= 14:
        return re.sub(r"\s*\n+\s*", " ", text).strip()
    return text


def _line_key(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def strip_repeated_lines(pages: Sequence[str], threshold: float = 0.5) -> list[str]:
    """Drop header and footer lines: lines identical (digits ignored, so ``Page 3`` = ``Page 4``) on more than
    ``threshold`` of the pages and at most 160 characters long. Needs at least 3 pages; fewer are returned as
    they are."""
    pages = list(pages)
    if len(pages) < 3:
        return pages
    counts: dict[str, int] = {}
    for page in pages:
        for key in {_line_key(ln) for ln in page.splitlines() if ln.strip() and len(ln.strip()) <= 160}:
            counts[key] = counts.get(key, 0) + 1
    repeated = {k for k, c in counts.items() if c > len(pages) * threshold}
    if not repeated:
        return pages
    return ["\n".join(ln for ln in page.splitlines() if _line_key(ln) not in repeated) for page in pages]


def useful_chars(text: object) -> int:
    """How many letters and digits ``text`` has."""
    return sum(1 for c in ("" if text is None else str(text)) if c.isalnum())


def decode_text(data: object, *, reject_binary: bool = False) -> str:
    """Bytes to text without guessing libraries: a BOM decides (UTF-8, UTF-16, UTF-32); otherwise strict UTF-8, then
    Windows-1252, then Latin-1 (never fails). A ``str`` is returned as it is. ``reject_binary`` raises
    ``ValueError`` when the first 4 KB hold a NUL byte and there is no UTF-16/32 BOM (so a PNG is not "decoded")."""
    if isinstance(data, str):
        return data
    b = bytes(data or b"")
    if b[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return b.decode("utf-32", errors="replace")
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", errors="replace")
    if reject_binary and b"\x00" in b[:4096]:
        raise ValueError("the data looks binary, not text")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return b.decode(encoding)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1")


def split_pages(text: str, size: int = 6000) -> list[str]:
    """Cut a long text into pieces of about ``size`` characters on paragraph breaks (a huge paragraph on line
    breaks). Short texts (up to ``2 * size``) come back as one piece; empty text as ``[]``."""
    text = clean_text(text, dehyphenate=False, unstack=False)
    if len(text) <= size * 2:
        return [text] if text else []
    pieces: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= size:
            pieces.append(para)
            continue
        chunk = ""
        for line in para.split("\n"):
            if chunk and len(chunk) + len(line) > size:
                pieces.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            pieces.append(chunk)
    pages: list[str] = []
    current = ""
    for para in pieces:
        if current and len(current) + len(para) > size:
            pages.append(current.strip())
            current = ""
        current += para + "\n\n"
    if current.strip():
        pages.append(current.strip())
    return pages
