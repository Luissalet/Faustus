"""src/browser_snapshot_file.py -- put a linked page snapshot back inline.

Recent ``@playwright/mcp`` builds no longer return the page's accessibility
snapshot in the tool result: they write it to ``.playwright-mcp/page-*.yml``
next to the server and answer with a link::

    ### Snapshot
    - [Snapshot](.playwright-mcp\\page-2026-10-22T21-36-32-048Z.yml)

The model cannot open that file, so every navigate / click / snapshot came
back blind (seen live 22-10: Wikipedia read through the browser, 51 KB on
disk, and the model got one line). This module swaps the link for the file's
contents in the shape older builds produced (``### Page snapshot`` plus a
yaml block), so the snapshot budget, paging, ref lookups and the answer all
work again.

Only files inside a ``.playwright-mcp`` folder with a snapshot extension are
read, and only up to ``MAX_BYTES``: a link in a tool result is never a way
to read an arbitrary file.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

MAX_BYTES = 2_000_000
_FOLDER = ".playwright-mcp"
_EXTENSIONS = (".yml", ".yaml", ".md", ".txt")

# "- [Snapshot](path)" -- optionally under a "### Snapshot" heading.
_LINK = re.compile(
    r"(?:^###[ \t]*(?:Page )?Snapshot[ \t]*\r?\n)?^[ \t]*-[ \t]*\[(?:Page )?Snapshot\]\(([^)\r\n]+)\)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _roots() -> Iterable[str]:
    seen = set()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for root in (os.getcwd(), here):
        if root and root not in seen:
            seen.add(root)
            yield root


def _resolve(link: str) -> Optional[str]:
    raw = (link or "").strip().strip("<>").replace("\\", os.sep).replace("/", os.sep)
    if not raw or "://" in raw:
        return None
    candidates = [raw] if os.path.isabs(raw) else [os.path.join(r, raw) for r in _roots()]
    for cand in candidates:
        path = os.path.realpath(cand)
        parts = [p.lower() for p in path.split(os.sep)]
        if _FOLDER not in parts[:-1]:
            continue
        if not path.lower().endswith(_EXTENSIONS):
            continue
        if os.path.isfile(path):
            return path
    return None


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return None
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_BYTES:
        text += "\n# ... (snapshot cut at %d bytes)" % MAX_BYTES
    return text


def inline_snapshot_links(text: str) -> str:
    """``text`` with each linked snapshot file replaced by its contents.

    A link that does not resolve to a readable snapshot file is left as it
    was, so nothing is lost.
    """
    if not isinstance(text, str) or "napshot](" not in text:
        return text

    def swap(m: "re.Match[str]") -> str:
        path = _resolve(m.group(1))
        body = _read(path) if path else None
        if body is None:
            return m.group(0)
        body = body.rstrip()
        if not body:
            body = "# (the page has no accessible content yet)"
        return f"### Page snapshot\n```yaml\n{body}\n```"

    return _LINK.sub(swap, text)
