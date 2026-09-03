"""Web provenance — anchor what the browser scraped to where it came from.

The problem
-----------
Page text that the integrated browser (FAUSTUS.md §16) hands the model arrives
as a wall of markdown with no way back to the source. The model then cites it
from memory, the user has no way to check the citation, and a paragraph that
changed under us since the fetch is indistinguishable from one that did not.

What we anchor by, and what we do NOT claim
-------------------------------------------
**The anchor is a character range in the fetched document plus a hash of the
text in that range. It is not a pixel coordinate, and this module will never
imply otherwise.** There is no tiled-screenshot pipeline here and no mapping
from text to screen position: what we honestly have is *offset a..b of the
document we fetched at time t, whose content hashed to h*. That is enough to
cite precisely ("characters 1240–1655 of https://…"), enough to detect that
the text moved or changed (the hash stops matching), and it is all we have. A
provenance record that claimed a bounding box would be a fabrication of the
same family as an invented page number, which ``src/expert_review.py`` refuses
for corpus citations.

The shape on the wire
---------------------
Every block carries one HTML comment immediately before it::

    <!-- source: https://example.com/a block=3 chars=1240-1655 sha256=9f2a1c0b77de -->

plus one document-level comment at the top naming the URL and the fetch time.
HTML comments are invisible in every markdown renderer and in the model's
reading of the prose, so the model-facing text stays readable;
:func:`strip_provenance` removes them again exactly, so
``strip_provenance(annotate(text, …)) == text`` byte for byte.

The offsets are into the **fetched document**, before annotation — the
annotated string is longer, and its own offsets are meaningless. That is why
:func:`verify_block` takes the original text as a separate argument.

Drift
-----
:func:`verify_block` recomputes each hash against the source it is given. A
block whose hash does not match is reported as **drifted**, with the range and
both hashes; it is never quietly accepted, and neither is a range that no
longer exists in the source.

Pure stdlib. Nothing here raises: bad input yields an empty result.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SETTING = "agent_web_provenance"
HASH_CHARS = 12

STATUS_MATCH = "match"
STATUS_DRIFTED = "drifted"

_BLOCK_RE = re.compile(
    r"<!--\s*source:\s*(?P<url>\S*)\s+block=(?P<block>\d+)\s+"
    r"chars=(?P<start>\d+)-(?P<end>\d+)\s+sha256=(?P<sha256>[0-9a-fA-F]+)\s*-->"
)
_DOC_RE = re.compile(
    r"<!--\s*provenance:\s*url=(?P<url>\S*)\s+fetched_at=(?P<fetched_at>\S*)\s+"
    r"blocks=(?P<blocks>\d+)\s*-->"
)
# Any provenance comment plus the newline annotate() always writes after it.
_STRIP_RE = re.compile(
    r"<!--\s*(?:source:|provenance:)[^>]*?-->\n"
)

_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
_MAX_URL = 2048


def enabled(settings: Optional[Mapping[str, Any]] = None) -> bool:
    """``agent_web_provenance`` (default true). Unreadable settings mean on:
    an anchor is additive and invisible, so the safe failure is to keep it."""
    if settings is not None:
        try:
            value = settings.get(SETTING, True)
        except AttributeError:
            return True
        return True if value is None else bool(value)
    try:
        from src.settings import get_setting
        return bool(get_setting(SETTING, True))
    except Exception:  # noqa: BLE001 - never raise on a settings read
        return True


def block_hash(text: Any) -> str:
    """First ``HASH_CHARS`` hex digits of the sha256 of ``text``."""
    try:
        raw = str(text or "")
    except Exception:  # noqa: BLE001
        raw = ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:HASH_CHARS]


def _clean_url(url: Any) -> str:
    text = str(url or "").strip()
    text = re.sub(r"\s+", "", text)
    return text[:_MAX_URL] or "unknown:"


def split_blocks(text: Any) -> List[Tuple[int, int]]:
    """Default blocking: paragraphs, split on blank lines, empties dropped.

    Ranges are line-aligned by construction, which is what keeps the inserted
    comment on a line of its own.
    """
    body = str(text or "")
    if not body.strip():
        return []
    out: List[Tuple[int, int]] = []
    cursor = 0
    for match in _BLANK_LINE_RE.finditer(body):
        end = match.start() + 1  # keep the block's own trailing newline out
        if body[cursor:end].strip():
            out.append((cursor, end))
        cursor = match.end()
    if body[cursor:].strip():
        out.append((cursor, len(body)))
    return out


def _normalise_ranges(text: str, blocks: Any) -> List[Tuple[int, int]]:
    """Accept ``[(start, end)]`` or ``[{"start":…, "end":…}]``; drop nonsense.

    Ranges are clamped into the document, sorted, and overlapping ones are
    dropped rather than merged: an anchor that overlaps another anchor cannot
    be verified independently, which is the only thing an anchor is for.
    """
    if blocks is None:
        return split_blocks(text)
    limit = len(text)
    raw: List[Tuple[int, int]] = []
    try:
        iterator: Iterable[Any] = list(blocks)
    except TypeError:
        return split_blocks(text)
    for item in iterator:
        try:
            if isinstance(item, Mapping):
                start, end = int(item.get("start")), int(item.get("end"))
            else:
                start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        start, end = max(0, min(start, limit)), max(0, min(end, limit))
        if end > start:
            raw.append((start, end))
    raw.sort()
    out: List[Tuple[int, int]] = []
    for start, end in raw:
        if out and start < out[-1][1]:
            continue
        out.append((start, end))
    return out


def annotate(markdown: Any, *, url: Any, fetched_at: Any = "",
             blocks: Any = None) -> str:
    """Return ``markdown`` with a provenance comment before every block.

    ``blocks`` is a sequence of ``(start, end)`` character ranges **into
    ``markdown`` as given**; ``None`` splits on blank lines. The returned text
    is longer than the input, so its own offsets are not the recorded ones —
    that asymmetry is deliberate and :func:`verify_block` takes the original
    document to check against.

    Never raises: anything it cannot annotate it returns unchanged.
    """
    try:
        body = str(markdown or "")
        if not body.strip():
            return body
        ranges = _normalise_ranges(body, blocks)
        if not ranges:
            return body
        clean_url = _clean_url(url)
        stamp = re.sub(r"\s+", "", str(fetched_at or "")) or "unknown"
        parts: List[str] = [
            f"<!-- provenance: url={clean_url} fetched_at={stamp} "
            f"blocks={len(ranges)} -->\n"
        ]
        cursor = 0
        for index, (start, end) in enumerate(ranges):
            parts.append(body[cursor:start])
            parts.append(
                f"<!-- source: {clean_url} block={index} chars={start}-{end} "
                f"sha256={block_hash(body[start:end])} -->\n"
            )
            parts.append(body[start:end])
            cursor = end
        parts.append(body[cursor:])
        return "".join(parts)
    except Exception as exc:  # noqa: BLE001 - annotation is additive, never fatal
        logger.debug("web provenance: annotate failed: %s", exc)
        return str(markdown or "")


def strip_provenance(text: Any) -> str:
    """Remove every provenance comment this module wrote, exactly."""
    try:
        return _STRIP_RE.sub("", str(text or ""))
    except Exception:  # noqa: BLE001
        return str(text or "")


def document_provenance(markdown: Any) -> Optional[Dict[str, Any]]:
    """The document-level record (url, fetched_at, block count), if present."""
    try:
        match = _DOC_RE.search(str(markdown or ""))
    except Exception:  # noqa: BLE001
        return None
    if not match:
        return None
    return {
        "url": match.group("url"),
        "fetched_at": match.group("fetched_at"),
        "blocks": int(match.group("blocks")),
    }


def extract_provenance(markdown: Any) -> List[Dict[str, Any]]:
    """Every block anchor in ``markdown``, in document order. Never raises."""
    out: List[Dict[str, Any]] = []
    try:
        body = str(markdown or "")
    except Exception:  # noqa: BLE001
        return out
    for match in _BLOCK_RE.finditer(body):
        try:
            out.append({
                "url": match.group("url"),
                "block": int(match.group("block")),
                "start": int(match.group("start")),
                "end": int(match.group("end")),
                "sha256": match.group("sha256").lower(),
            })
        except (TypeError, ValueError):
            continue
    return out


def verify_block(markdown: Any, source_text: Any) -> Dict[str, Any]:
    """Re-hash every anchor in ``markdown`` against ``source_text``.

    ``source_text`` is the document that was fetched — the one the offsets were
    taken in, NOT the annotated string.

    Returns ``{"ok", "checked", "matched", "drifted", "blocks": [...]}`` where
    every block row carries ``status`` (``match`` / ``drifted``), the recorded
    and the actual hash, and a ``why`` for anything that is not a match. A
    block that cannot be re-read (its range runs past the end of the source) is
    ``drifted`` too: an anchor that cannot be checked has not been verified,
    and this module will not call that a match.
    """
    source = str(source_text or "")
    records = extract_provenance(markdown)
    rows: List[Dict[str, Any]] = []
    drifted: List[int] = []
    for record in records:
        start, end = record["start"], record["end"]
        row: Dict[str, Any] = {
            "block": record["block"],
            "url": record["url"],
            "start": start,
            "end": end,
            "sha256": record["sha256"],
            "actual_sha256": "",
            "status": STATUS_DRIFTED,
            "why": "",
        }
        if start > end or end > len(source):
            row["why"] = (f"characters {start}-{end} are past the end of the source "
                          f"({len(source)} characters)")
            drifted.append(record["block"])
        else:
            actual = block_hash(source[start:end])
            row["actual_sha256"] = actual
            if actual == record["sha256"]:
                row["status"] = STATUS_MATCH
            else:
                row["why"] = "the text in that range is not the text that was fetched"
                drifted.append(record["block"])
        rows.append(row)
    return {
        "ok": not drifted,
        "checked": len(rows),
        "matched": len(rows) - len(drifted),
        "drifted": drifted,
        "blocks": rows,
    }
