"""src/browser_snapshot_window.py — what the model sees of a big page snapshot.

A browser snapshot of a long page does not fit a small model's context and
cutting it at a fixed size loses everything after the cut. This module keeps
the FULL latest snapshot of each browser connection out of the prompt and
gives the model three ways to reach the rest without paying for all of it:

* **New-element marks.** When the same page is snapshotted again, elements
  whose ``(role, name, ref)`` was not in the previous snapshot of that page
  get a ``*`` after their list marker, so after a click the model can see what
  the click made appear instead of diffing two trees in its head.
* **Windows.** Instead of one cut at ``browser_snapshot_max_chars``, the
  snapshot is served in windows of that size. Every window ends with the
  page's navigation links (repeated, bounded) and a line telling the model
  which ``page_window`` call fetches the next one.
* **Search.** ``page_find`` searches the whole stored snapshot for a string or
  a regular expression and returns the matching lines with their refs and a
  little context, never the page.

Nothing here touches the network or the browser: it only post-processes text
that already came back from the browser server, and keeps it in a small,
bounded, in-memory table keyed by the MCP connection id (so a session's own
browser and the shared one never see each other's pages).
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.browser_view import _ELEMENT_REF_RE, parse_page_info, parse_snapshot_elements

NEW_MARK = "*"
NAV_CHARS = 600
MIN_WINDOW = 500
MAX_STORED_CHARS = 2_000_000
MAX_PAGES_TRACKED = 48
MAX_CONNECTIONS = 16
FIND_MAX_MATCHES = 50
FIND_DEFAULT_MATCHES = 10
FIND_MAX_CONTEXT = 5
FIND_OUTPUT_CHARS = 6000
_LINE_CLIP = 240
_MAX_PATTERN_LEN = 300

_LIST_MARKER_RE = re.compile(r"^(\s*(?:[-*•]|\d{1,4}[.)])\s*)")
_NAV_ROLE_RE = re.compile(r"^\s*(?:[-*•]|\d{1,4}[.)])?\s*(?:\*\s*)?(navigation|banner)\b", re.IGNORECASE)
_WINDOW_NOTE_RE = re.compile(r"^\[window: chars (\d+)-(\d+) of (\d+)", re.MULTILINE)


@dataclass
class Snapshot:
    server_id: str
    url: str
    title: str
    raw: str                    # the text as the browser returned it
    shown: str                  # the text with new-element marks (what windows slice)
    snapshot_id: str
    nav: str = ""
    new_count: Optional[int] = None    # None = no previous snapshot of this page to compare
    taken_at: float = field(default_factory=time.time)


_LOCK = threading.Lock()
_PAGES: "OrderedDict[Tuple[str, str], frozenset]" = OrderedDict()
_LATEST: "OrderedDict[str, Snapshot]" = OrderedDict()


def reset() -> None:
    """Forget everything (tests, and a browser restart)."""
    with _LOCK:
        _PAGES.clear()
        _LATEST.clear()


def forget_connection(server_id: str) -> None:
    with _LOCK:
        _LATEST.pop(server_id, None)
        for key in [k for k in _PAGES if k[0] == server_id]:
            _PAGES.pop(key, None)


def _page_key(url: str) -> str:
    return (url or "").split("#", 1)[0]


def _signatures(text: str) -> frozenset:
    return frozenset((e["role"], e["name"], e["ref"]) for e in parse_snapshot_elements(text))


# ---------------------------------------------------------------------------
# New-element marks
# ---------------------------------------------------------------------------

def mark_new_elements(text: str, previous: frozenset) -> Tuple[str, int]:
    """Insert ``* `` after the list marker of every element line whose
    (role, name, ref) is not in `previous`. Lines without a ref are untouched.
    Returns (marked text, number of new elements)."""
    out: List[str] = []
    count = 0
    for line in text.split("\n"):
        if "ref" not in line or not _ELEMENT_REF_RE.search(line):
            out.append(line)
            continue
        elements = parse_snapshot_elements(line)
        if not elements:
            out.append(line)
            continue
        e = elements[0]
        if (e["role"], e["name"], e["ref"]) in previous:
            out.append(line)
            continue
        m = _LIST_MARKER_RE.match(line)
        if m:
            out.append(f"{m.group(1).rstrip()} {NEW_MARK} {line[m.end():]}")
        else:
            indent = line[: len(line) - len(line.lstrip(" "))]
            out.append(f"{indent}{NEW_MARK} {line.lstrip(' ')}")
        count += 1
    return "\n".join(out), count


# ---------------------------------------------------------------------------
# Navigation block, repeated at the end of every window
# ---------------------------------------------------------------------------

def nav_block(text: str, max_chars: int = NAV_CHARS) -> str:
    """The first `max_chars` characters of the page's navigation landmarks
    (their subtrees, by indentation); when the page has none, its first few
    links. Empty string when neither exists."""
    lines = text.split("\n")
    picked: List[str] = []
    size = 0

    def take(line: str) -> bool:
        nonlocal size
        clipped = line if len(line) <= _LINE_CLIP else line[:_LINE_CLIP] + " ..."
        if size + len(clipped) + 1 > max_chars and picked:
            return False
        picked.append(clipped)
        size += len(clipped) + 1
        return True

    i = 0
    full = False
    while i < len(lines) and not full:
        if _NAV_ROLE_RE.match(lines[i]):
            base = len(lines[i]) - len(lines[i].lstrip(" "))
            if not take(lines[i]):
                break
            j = i + 1
            while j < len(lines):
                raw = lines[j]
                if raw.strip():
                    indent = len(raw) - len(raw.lstrip(" "))
                    if indent <= base:
                        break
                    if not take(raw):
                        full = True
                        break
                j += 1
            i = j
        else:
            i += 1
    if picked:
        return "\n".join(picked)
    links = [ln for ln in lines if re.match(r"^\s*(?:[-*•]|\d{1,4}[.)])?\s*(?:\*\s*)?link\b", ln, re.IGNORECASE)]
    for ln in links[:12]:
        if not take(ln):
            break
    return "\n".join(picked)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _line_starts(text: str) -> List[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def cut_window(text: str, offset: int, limit: int) -> Tuple[str, int, int]:
    """(body, start, end): whole lines of `text` from the line holding
    `offset` up to `limit` characters. Always makes progress: a single line
    longer than `limit` is clipped."""
    limit = max(MIN_WINDOW, int(limit))
    n = len(text)
    offset = max(0, min(int(offset), n))
    if offset > 0 and offset < n and text[offset - 1] != "\n":
        offset = text.rfind("\n", 0, offset) + 1
    if offset >= n:
        return "", n, n
    end = offset
    while end < n:
        nl = text.find("\n", end)
        line_end = n if nl == -1 else nl + 1
        if line_end - offset > limit:
            if end == offset:
                # one giant line: clip it so the window advances
                return text[offset:offset + limit] + " ...[line clipped]", offset, line_end
            break
        end = line_end
    return text[offset:end].rstrip("\n"), offset, end


def render_window(snap: Snapshot, offset: int, limit: int) -> str:
    body, start, end = cut_window(snap.shown, offset, limit)
    total = len(snap.shown)
    parts: List[str] = []
    if start > 0:
        parts.append(f"### Page window of {snap.url or '(unknown page)'}")
    parts.append(body)
    if snap.nav and snap.nav.split("\n", 1)[0] not in body:
        parts.append("--- page navigation (repeated in every window) ---\n" + snap.nav)
    if snap.new_count is not None and start == 0:
        parts.append(f"(`{NEW_MARK}` marks elements new since the previous snapshot of this page: "
                     f"{snap.new_count if snap.new_count else 'none'})")
    if end < total:
        parts.append(
            f"[window: chars {start}-{end} of {total} (snapshot {snap.snapshot_id}). "
            f"Next window: page_window {{\"offset\": {end}}}. To look for something specific "
            f"without reading the page: page_find {{\"query\": \"...\"}}]"
        )
    elif start > 0:
        parts.append(f"[window: chars {start}-{end} of {total} (snapshot {snap.snapshot_id}). "
                     f"This is the last window.]")
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# The entry point used by the MCP manager
# ---------------------------------------------------------------------------

def process_snapshot(server_id: str, text: str, *, limit: int, mark_new: bool,
                     paging: bool) -> Tuple[str, bool]:
    """Store `text` as the latest snapshot of `server_id` and return
    ``(text_for_the_model, handled)``. `handled` is True when the returned text
    is already bounded by windowing (the caller must not cut it again)."""
    if not isinstance(text, str) or not text:
        return text, False
    server_id = str(server_id or "")
    url, title = parse_page_info(text)
    key = (server_id, _page_key(url))
    shown = text
    new_count: Optional[int] = None
    if mark_new and "ref" in text:
        with _LOCK:
            previous = _PAGES.get(key)
        if previous is not None:
            shown, new_count = mark_new_elements(text, previous)
        sigs = _signatures(text)
        with _LOCK:
            _PAGES[key] = sigs
            _PAGES.move_to_end(key)
            while len(_PAGES) > MAX_PAGES_TRACKED:
                _PAGES.popitem(last=False)
    stored = text if len(text) <= MAX_STORED_CHARS else text[:MAX_STORED_CHARS]
    snap = Snapshot(
        server_id=server_id, url=url, title=title, raw=stored,
        shown=shown if len(shown) <= MAX_STORED_CHARS else shown[:MAX_STORED_CHARS],
        snapshot_id=hashlib.sha1(shown.encode("utf-8", "replace")).hexdigest()[:8],
        nav=nav_block(text) if paging and len(shown) > max(MIN_WINDOW, int(limit)) else "",
        new_count=new_count,
    )
    with _LOCK:
        _LATEST[server_id] = snap
        _LATEST.move_to_end(server_id)
        while len(_LATEST) > MAX_CONNECTIONS:
            _LATEST.popitem(last=False)
    if paging and len(shown) > max(MIN_WINDOW, int(limit)):
        return render_window(snap, 0, limit), True
    if new_count is not None:
        note = (f"\n\n(`{NEW_MARK}` marks elements new since the previous snapshot of this page: "
                f"{new_count if new_count else 'none'})")
        return shown + note, False
    return shown, False


def latest(server_ids: List[str]) -> Optional[Snapshot]:
    """The most recent stored snapshot among the first of `server_ids` that has one."""
    with _LOCK:
        for sid in server_ids:
            snap = _LATEST.get(sid)
            if snap is not None:
                return snap
    return None


def full_text_for(window_text: str) -> Optional[str]:
    """When `window_text` is one window of a stored snapshot, the complete raw
    snapshot of the same page (so a precondition check can look for an element
    that sits in another window). None when it is not a window or the stored
    snapshot is not the one it came from."""
    if not isinstance(window_text, str) or "[window: chars " not in window_text:
        return None
    url, _title = parse_page_info(window_text)
    m = re.search(r"\(snapshot ([0-9a-f]{8})\)", window_text)
    if not m:
        return None
    with _LOCK:
        for snap in reversed(list(_LATEST.values())):
            if snap.snapshot_id == m.group(1) and (not url or snap.url == url or not snap.url):
                return snap.raw
    return None


# ---------------------------------------------------------------------------
# page_find
# ---------------------------------------------------------------------------

class FindError(ValueError):
    pass


def _clip(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= _LINE_CLIP else line[:_LINE_CLIP] + " ..."


def find_in_snapshot(snap: Snapshot, query: str, *, regex: bool = False,
                     case_sensitive: bool = False, context: int = 1,
                     max_matches: int = FIND_DEFAULT_MATCHES) -> Dict[str, Any]:
    query = str(query or "")
    if not query.strip():
        raise FindError("`query` is required")
    if len(query) > _MAX_PATTERN_LEN:
        raise FindError(f"`query` is longer than {_MAX_PATTERN_LEN} characters")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(query if regex else re.escape(query), flags)
    except re.error as exc:
        raise FindError(f"`query` is not a valid regular expression: {exc}") from exc
    if regex and rx.match(""):
        raise FindError("the expression matches the empty string; it would match every line")
    context = max(0, min(int(context), FIND_MAX_CONTEXT))
    max_matches = max(1, min(int(max_matches), FIND_MAX_MATCHES))
    lines = snap.raw.split("\n")
    matches: List[Dict[str, Any]] = []
    total = 0
    last_ref = ""
    last_ref_line = -1
    for i, line in enumerate(lines):
        rm = _ELEMENT_REF_RE.search(line)
        if rm:
            last_ref, last_ref_line = rm.group(1), i
        if not rx.search(line):
            continue
        total += 1
        if len(matches) >= max_matches:
            continue
        ref = rm.group(1) if rm else ""
        entry: Dict[str, Any] = {
            "line": i + 1, "text": _clip(line.strip()), "ref": ref,
            "before": [_clip(x.strip()) for x in lines[max(0, i - context):i] if x.strip()],
            "after": [_clip(x.strip()) for x in lines[i + 1:i + 1 + context] if x.strip()],
        }
        if not ref and last_ref:
            entry["under_ref"] = last_ref
            entry["lines_below_ref"] = i - last_ref_line
        matches.append(entry)
    return {"matches": matches, "total_matches": total, "returned": len(matches),
            "snapshot_id": snap.snapshot_id, "url": snap.url, "title": snap.title,
            "chars": len(snap.raw)}


def format_find(query: str, result: Dict[str, Any]) -> str:
    head = (f"page_find {query!r}: {result['total_matches']} match(es) in {result['chars']} chars "
            f"(snapshot {result['snapshot_id']}, {result['url'] or 'unknown page'})")
    if not result["matches"]:
        return head + ". Nothing matched; try a shorter string or a regex."
    out = [head + (f"; showing the first {result['returned']}" if result["total_matches"] > result["returned"] else "")]
    used = len(out[0])
    for n, m in enumerate(result["matches"], start=1):
        block = []
        for b in m["before"]:
            block.append(f"     {b}")
        tag = f"[ref={m['ref']}] " if m["ref"] else (f"(under ref={m['under_ref']}, {m['lines_below_ref']} line(s) below) "
                                                      if m.get("under_ref") else "")
        block.append(f"{n}. line {m['line']} {tag}> {m['text']}")
        for a in m["after"]:
            block.append(f"     {a}")
        chunk = "\n".join(block)
        if used + len(chunk) > FIND_OUTPUT_CHARS:
            out.append(f"(output limit reached after {n - 1} match(es); narrow the query)")
            break
        out.append(chunk)
        used += len(chunk) + 1
    return "\n".join(out)


__all__ = [
    "Snapshot", "FindError", "reset", "forget_connection", "mark_new_elements", "nav_block",
    "cut_window", "render_window", "process_snapshot", "latest", "full_text_for",
    "find_in_snapshot", "format_find", "NEW_MARK",
]
