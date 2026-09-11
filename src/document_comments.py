"""document_comments.py — anchored comments and accept-at-anchor proposals
on Documents (ADP-05, W1-E).

A comment does not point at a character offset (stale after the very next
edit) or at "the first occurrence of this text" (wrong the moment the text
repeats). It points at a **quote plus its immediate surrounding context**
(`before_ctx`/`after_ctx`), and `relocate()` re-finds that same span in
whatever the document's content is *right now*. Three outcomes, never a
guess in between:

- The quote occurs exactly once -> relocated there, unambiguous.
- The quote occurs more than once, but `before_ctx`/`after_ctx` narrow it
  down to exactly one occurrence -> relocated there.
- The quote is gone, or occurs more than once and context does not
  disambiguate -> `orphan`. An orphaned comment is never pinned to a
  different paragraph just because it has to point somewhere.

`accept()` never trusts the comment's *stored* `structural_pos` — it always
relocates against the CURRENT content first, then applies `proposal.find ->
proposal.replace` only inside that freshly-relocated span, never as a
document-wide first-occurrence replace. If the anchor cannot be relocated
(quote gone, or now ambiguous) `accept()` fails closed with
`document_comments.base_changed`: a human's edit since the comment was
raised is never silently overwritten by an accept that was decided against
older text.

Comments do not carry any privileged instruction status: `body`/`proposal`
are opaque text to every caller here — nothing in this module or in
`routes/document_comments_routes.py` feeds comment text back into a prompt
or executes it (CONTRATO_ADP_W1 "no convertir comentarios en instrucciones
privilegiadas").
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

STATE_OPEN = "open"
STATE_RESOLVED = "resolved"
STATE_ORPHAN = "orphan"

#: How much of the immediate surrounding text counts as "context" when a
#: quote repeats. Small on purpose: this disambiguates "the same sentence
#: appears in two similar paragraphs", not "find the right chapter".
_CTX_CHARS = 40


# ---------------------------------------------------------------------------
# Markdown block indexing (structural_pos)
# ---------------------------------------------------------------------------

_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")


def block_spans(text: str) -> List[Tuple[int, int]]:
    """Offsets of each blank-line-separated markdown block in `text`.

    Best-effort and deliberately simple (no markdown parser): `structural_pos`
    is a UI hint for "roughly which block", not a contract anything else
    relies on for correctness — relocation always re-derives position from
    the quote itself, never from a stored block index."""
    if not text:
        return [(0, 0)]
    spans: List[Tuple[int, int]] = []
    pos = 0
    for m in _BLOCK_SPLIT_RE.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    return spans


def block_index_for_offset(text: str, offset: int) -> int:
    spans = block_spans(text)
    for i, (start, end) in enumerate(spans):
        if start <= offset <= end:
            return i
    return max(0, len(spans) - 1)


# ---------------------------------------------------------------------------
# Anchor location
# ---------------------------------------------------------------------------

@dataclass
class LocateResult:
    status: str              # "found" | "not_found" | "ambiguous"
    start: int = -1
    end: int = -1


def _find_all(text: str, quote: str) -> List[int]:
    if not quote:
        return []
    out = []
    start = 0
    while True:
        i = text.find(quote, start)
        if i == -1:
            break
        out.append(i)
        start = i + 1  # allow overlapping occurrences, never skip one
    return out


def locate_quote(text: str, quote: str, before_ctx: str = "", after_ctx: str = "") -> LocateResult:
    """Find where `quote` sits in `text`, using `before_ctx`/`after_ctx` to
    disambiguate a repeated quote. Never picks "the first match" when more
    than one occurrence remains after context is applied."""
    if not quote:
        return LocateResult(status="not_found")
    occurrences = _find_all(text, quote)
    if not occurrences:
        return LocateResult(status="not_found")
    if len(occurrences) == 1:
        i = occurrences[0]
        return LocateResult(status="found", start=i, end=i + len(quote))

    # Repeated quote: keep only occurrences whose immediate surrounding text
    # matches the recorded context. An empty before_ctx/after_ctx matches
    # only an occurrence that itself sits at a text boundary — it does not
    # wildcard-match everything, or a repeated quote with no context would
    # resolve to "found" at every one of them.
    matches = []
    for i in occurrences:
        end = i + len(quote)
        actual_before = text[max(0, i - len(before_ctx)):i]
        actual_after = text[end:end + len(after_ctx)]
        if actual_before == before_ctx and actual_after == after_ctx:
            matches.append(i)
    if len(matches) == 1:
        i = matches[0]
        return LocateResult(status="found", start=i, end=i + len(quote))
    return LocateResult(status="ambiguous")


def context_for(text: str, start: int, end: int) -> Tuple[str, str]:
    """The `(before_ctx, after_ctx)` pair to store for a freshly anchored
    quote spanning `[start, end)` in `text` — up to `_CTX_CHARS` on each
    side, capped by the document's own boundaries."""
    before = text[max(0, start - _CTX_CHARS):start]
    after = text[end:end + _CTX_CHARS]
    return before, after


# ---------------------------------------------------------------------------
# Relocation against a document's current content
# ---------------------------------------------------------------------------

def relocate(comment, new_text: str) -> Dict[str, Any]:
    """Re-anchor one `DocumentComment` row against `new_text`.

    Returns a dict of the fields to write back (`state`, `structural_pos`,
    and — only when the match still needed the OLD context to disambiguate
    — nothing else: `quote`/`before_ctx`/`after_ctx` themselves are never
    rewritten by a relocate, so a later relocation is judged against the
    same anchor the comment was created with, not a moving target)."""
    result = locate_quote(new_text, comment.quote, comment.before_ctx or "", comment.after_ctx or "")
    if result.status != "found":
        return {"state": STATE_ORPHAN, "structural_pos": None}
    pos = block_index_for_offset(new_text, result.start)
    state = STATE_RESOLVED if comment.state == STATE_RESOLVED else STATE_OPEN
    return {"state": state, "structural_pos": pos, "_start": result.start, "_end": result.end}


def relocate_comments_for_document(sa_db, document) -> List[Dict[str, Any]]:
    """Re-anchor every non-resolved comment on `document` against its
    CURRENT `current_content`. Called from the document save path right
    after a successful write — the same hook point `document_links` uses —
    so an insert-before-the-comment edit keeps the comment anchored (the
    quote is still found, just at a new offset) and a since-orphaned quote
    is marked, never silently dropped or reattached elsewhere."""
    from core.database import DocumentComment
    text = document.current_content or ""
    rows = (sa_db.query(DocumentComment)
            .filter(DocumentComment.document_id == document.id,
                    DocumentComment.state.in_((STATE_OPEN, STATE_ORPHAN)))
            .all())
    out = []
    for c in rows:
        upd = relocate(c, text)
        c.state = upd["state"]
        c.structural_pos = upd["structural_pos"]
        c.base_version = document.version_count
        out.append({"id": c.id, "state": c.state, "structural_pos": c.structural_pos})
    return out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CommentError(ValueError):
    def __init__(self, message: str, error_class: str = "document_comments.invalid"):
        super().__init__(message)
        self.error_class = error_class


class BaseChangedError(CommentError):
    def __init__(self, message: str = "The document changed since this comment was anchored."):
        super().__init__(message, "document_comments.base_changed")


# ---------------------------------------------------------------------------
# Create / accept
# ---------------------------------------------------------------------------

def new_comment(document, *, quote: str, body: str, author: str = "human",
                 proposal_find: Optional[str] = None, proposal_replace: Optional[str] = None,
                 before_ctx: Optional[str] = None, after_ctx: Optional[str] = None):
    """Build a `DocumentComment` row anchored against `document`'s CURRENT
    content. Raises `BaseChangedError` (repurposed here as "anchor not
    found") if `quote` is not locatable, so a comment is never created
    pointing at nothing."""
    from core.database import DocumentComment
    text = document.current_content or ""
    if before_ctx is None or after_ctx is None:
        result = locate_quote(text, quote)
        if result.status == "not_found":
            raise CommentError("quote not found in document", "document_comments.quote_not_found")
        if result.status == "ambiguous":
            raise CommentError(
                "quote occurs more than once; pass before_ctx/after_ctx to disambiguate",
                "document_comments.quote_ambiguous")
        before_ctx, after_ctx = context_for(text, result.start, result.end)
        pos = block_index_for_offset(text, result.start)
    else:
        result = locate_quote(text, quote, before_ctx, after_ctx)
        pos = block_index_for_offset(text, result.start) if result.status == "found" else None

    return DocumentComment(
        id=str(uuid.uuid4()),
        document_id=document.id,
        owner=document.owner,
        base_version=document.version_count,
        quote=quote,
        before_ctx=before_ctx or "",
        after_ctx=after_ctx or "",
        structural_pos=pos,
        body=body or "",
        author=author if author in ("human", "model") else "human",
        state=STATE_OPEN,
        proposal_find=proposal_find,
        proposal_replace=proposal_replace,
    )


def accept(sa_db, document, comment) -> str:
    """Apply `comment.proposal_find -> proposal_replace` at THIS comment's
    anchor, re-located against `document.current_content` right now, and
    persist the result onto `document` (new `DocumentVersion`, bumped
    `version_count` — the same shape `update_document` uses for a user
    edit). Returns the new content. Raises `BaseChangedError` if the anchor
    cannot be relocated, or if `proposal_find` is no longer present inside
    the relocated span — either way, a stale accept never touches content a
    human has since changed.
    """
    if not comment.proposal_find:
        raise CommentError("comment has no proposal to accept", "document_comments.no_proposal")
    text = document.current_content or ""
    result = locate_quote(text, comment.quote, comment.before_ctx or "", comment.after_ctx or "")
    if result.status != "found":
        raise BaseChangedError()
    window = text[result.start:result.end]
    idx = window.find(comment.proposal_find)
    if idx == -1:
        raise BaseChangedError()
    replace = comment.proposal_replace or ""
    new_window = window[:idx] + replace + window[idx + len(comment.proposal_find):]
    new_content = text[:result.start] + new_window + text[result.end:]

    _apply_document_edit(sa_db, document, new_content, summary=f"Accepted comment {comment.id}")
    comment.state = STATE_RESOLVED
    comment.structural_pos = block_index_for_offset(new_content, result.start)
    comment.base_version = document.version_count
    return new_content


def _apply_document_edit(sa_db, document, new_content: str, *, summary: str) -> None:
    """Write `new_content` onto `document` as a new version. Mirrors the
    non-coalescing branch of `routes/document/document_routes.py::update_document`
    (new `DocumentVersion`, bumped `version_count`) — deliberately NOT
    imported from there to avoid a route-module -> this-module import cycle
    (the route module hooks INTO this one, not the other way round); kept
    minimal on purpose since it is the one piece of document-write logic
    this module owns."""
    from core.database import DocumentVersion
    if document.current_content == new_content:
        return
    new_ver = (document.version_count or 1) + 1
    sa_db.add(DocumentVersion(
        id=str(uuid.uuid4()),
        document_id=document.id,
        version_number=new_ver,
        content=new_content,
        summary=summary,
        source="user",
    ))
    document.version_count = new_ver
    document.current_content = new_content
