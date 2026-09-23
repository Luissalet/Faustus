"""
brain/wikilinks.py — `[[wiki links]]`, `![[embeds]]` and inline `#tags`.

Pure text functions on purpose: the vault indexer (`notes.reindex`) turns
these into rows, `vault.sync` uses `rewrite_target` to keep other notes
pointing at a renamed file, and both need identical parsing to agree on what
a link "is" without importing each other.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

#: `[[target]]`, `[[target|label]]`, `[[target#heading]]`,
#: `[[target#heading|label]]`, and the embed form `![[...]]` — everything up
#: to `#`/`|`/`]` belongs to the target, so a target itself never holds
#: those characters (matches how the vault names files: safe_filename()
#: already strips `#`, `|`, `[`, `]`).
_LINK_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\]\|#]+?)(?:#(?P<heading>[^\]\|]+?))?(?:\|(?P<label>[^\]]+?))?\]\]"
)

#: An inline tag: `#word` not preceded by another `#` or a word character —
#: excludes ATX headings (`# Title`, always followed by a space) and a
#: second `#` in `##`.
_TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z0-9_][A-Za-z0-9_/-]*)")


def parse(body: Any) -> List[Dict[str, Any]]:
    """Every link/embed in `body`, in document order."""
    out: List[Dict[str, Any]] = []
    for m in _LINK_RE.finditer(str(body or "")):
        out.append({
            "target": m.group("target").strip(),
            "label": (m.group("label") or "").strip(),
            "heading": (m.group("heading") or "").strip(),
            "is_embed": bool(m.group("embed")),
        })
    return out


def tags(body: Any, fm: Any = None) -> List[str]:
    """Frontmatter `tags:` plus inline `#tags` in the body, deduped, sorted."""
    found = {m.group(1) for m in _TAG_RE.finditer(str(body or ""))}
    fm = fm or {}
    for tag in fm.get("tags") or []:
        text = str(tag or "").strip().lstrip("#")
        if text:
            found.add(text)
    return sorted(found)


def rewrite_target(body: Any, old: str, new: str) -> str:
    """Every link whose target is exactly `old` (e.g. a renamed file's old
    basename) now points at `new`; heading, label and embed-ness untouched."""
    old = str(old or "")
    new = str(new or "")
    if not old or old == new:
        return str(body or "")

    def _sub(m: "re.Match[str]") -> str:
        if m.group("target").strip() != old:
            return m.group(0)
        pieces = ("!" if m.group("embed") else "") + "[[" + new
        if m.group("heading"):
            pieces += "#" + m.group("heading")
        if m.group("label"):
            pieces += "|" + m.group("label")
        return pieces + "]]"

    return _LINK_RE.sub(_sub, str(body or ""))
