"""
project_context/resolvers/document.py — living documents as project sources.

Reads ``Document`` / ``DocumentVersion`` from ``core/database.py``. Three
things about that schema are easy to get wrong, and each has already cost
somebody a bug:

* **The live text is ``Document.current_content``**, not the newest
  ``DocumentVersion``. Versions are snapshots written on edit; the current text
  is the column. Reading the last version row returns the state *before* the
  most recent edit.
* **The version number is ``Document.version_count``**, a monotonic integer.
  There is **no ``is_current`` flag on ``DocumentVersion``** — a resolver that
  filters on one silently returns nothing.
* **Ownership is ``Document.owner``, the column — never ``session_id``.**
  ``documents.session_id`` is ``ON DELETE SET NULL``: deleting a chat orphans
  its documents but does not delete them, and a resolver that derives ownership
  from the session makes every one of those documents belong to nobody, which
  in practice means it refuses the owner their own file.

Obligations of every resolver (see ``base.py``): owner checked before the
source is touched; ``missing``/``forbidden`` leak nothing; stable revision;
bounded reads; a stored label is never access control.

Revision string::

    document:<doc_id>:v<version_count>:<sha256 of the effective content>

Version policies:

* ``latest``  — ``current_content`` at ``version_count``.
* ``pinned``  — the ``DocumentVersion`` whose ``version_number`` matches. If
  there is no such row the answer is ``missing``, never the nearest one:
  "pinned to version 3" quietly resolving to version 2 is how an approved
  requirements document turns into a draft.
* ``snapshot`` — ``unsupported``. It needs an immutable Artifact materialised
  from the document, and that write path does not exist yet.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, List, Mapping, Optional, Tuple

from ..models import (
    ExtractedChunk, ExtractedCorpus, SourceContent, SourceMatch, SourceMetadata, SourceRef,
)
from .base import ResolverBase

logger = logging.getLogger(__name__)

__all__ = ["DocumentResolver", "chunk_markdown"]

#: Target size of an indexable chunk, in characters. Headings are respected
#: first; this only splits a section that has no heading to split on.
CHUNK_CHARS = 4000

_SNAPSHOT_NOTE = (
    "version_policy 'snapshot' is not implemented for documents yet: it requires "
    "materialising an immutable Artifact from the document and linking that copy. "
    "Use 'pinned' with a version number for a fixed revision."
)


def chunk_markdown(body: str, *, max_chars: int = CHUNK_CHARS) -> List[Tuple[str, str]]:
    """Split on Markdown headings, then on blank lines when a section is long.

    Returns ``(title, text)`` pairs. Headings first because a heading is the
    cheapest real title a chunk can carry, and a chunk with a title can be
    cited back to the user as a place rather than as an offset.
    """
    lines = (body or "").splitlines()
    sections: List[Tuple[str, List[str]]] = [("", [])]
    for line in lines:
        if line.startswith("#") and line.lstrip("#").startswith(" "):
            sections.append((line.lstrip("#").strip(), [line]))
        else:
            sections[-1][1].append(line)
    out: List[Tuple[str, str]] = []
    for title, block in sections:
        joined = "\n".join(block).strip()
        if not joined:
            continue
        if len(joined) <= max_chars:
            out.append((title, joined))
            continue
        buffer: List[str] = []
        size = 0
        for para in joined.split("\n\n"):
            if size and size + len(para) > max_chars:
                out.append((title, "\n\n".join(buffer).strip()))
                buffer, size = [], 0
            buffer.append(para)
            size += len(para) + 2
        if buffer:
            out.append((title, "\n\n".join(buffer).strip()))
    return out


class DocumentResolver(ResolverBase):
    """Resolver for ``kind='document'``.

    ``session_factory`` exists so a test can hand in its own sessionmaker
    without monkeypatching an import; production leaves it None and the
    resolver reads ``core.database.SessionLocal`` at call time (late, so a test
    that swaps that attribute still wins).
    """

    kind = "document"

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory

    # ── plumbing ───────────────────────────────────────────────────────────

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from core.database import SessionLocal  # late import: keeps import cost off
        return SessionLocal()

    @staticmethod
    def _models():
        from core.database import Document, DocumentVersion
        return Document, DocumentVersion

    @staticmethod
    def _digest(body: str) -> str:
        return hashlib.sha256((body or "").encode("utf-8", "replace")).hexdigest()

    def _effective(self, doc, version_policy: str, pinned_version: Optional[int],
                   db) -> "Tuple[Optional[str], int, str]":
        """The content, version number and state for a policy.

        Returns ``(content, version, state)`` with ``content=None`` whenever the
        state is not ``ok`` — so a caller that ignores the state still cannot
        read anything.
        """
        policy = (version_policy or "latest").strip() or "latest"
        if policy == "snapshot":
            return None, 0, "unsupported"
        if policy == "pinned":
            if not pinned_version:
                return None, 0, "missing"
            _, DocumentVersion = self._models()
            row = (db.query(DocumentVersion)
                     .filter(DocumentVersion.document_id == doc.id,
                             DocumentVersion.version_number == int(pinned_version))
                     .first())
            if row is None:
                # Not the nearest version. See the module docstring.
                return None, 0, "missing"
            return (row.content or ""), int(row.version_number or 0), "ok"
        return (doc.current_content or ""), int(doc.version_count or 0), "ok"

    def _owned(self, db, doc_id: str, owner: str):
        """The row, or ``(None, state)``. Ownership is decided here, before any
        content is read out of the row."""
        Document, _ = self._models()
        doc = db.query(Document).filter(Document.id == str(doc_id or "")).first()
        if doc is None:
            return None, "missing"
        if not owner or (doc.owner or "") != owner:
            # Same shape as "missing" to the caller — see obligation 2. The two
            # states differ only so the *owner* can be told which one it was.
            return None, "forbidden"
        return doc, "ok"

    # ── the protocol ───────────────────────────────────────────────────────

    def policy_note(self, version_policy: str) -> str:
        return _SNAPSHOT_NOTE if (version_policy or "") == "snapshot" else ""

    def validate(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        return self.metadata(ref, owner=owner, project=project)

    def metadata(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        doc_id = (getattr(ref, "id", "") or "").strip()
        if not doc_id:
            return self._denied("missing", "no document id was given")
        try:
            db = self._session()
        except Exception as exc:  # noqa: BLE001 - retrieval path, never raises
            logger.warning("document resolver could not open a session: %s", exc)
            return self._denied("missing", "the document store is unavailable")
        try:
            doc, state = self._owned(db, doc_id, owner)
            if doc is None:
                return self._denied(state, "not found" if state == "missing"
                                    else "not owned by the caller")
            body = doc.current_content or ""
            language = (doc.language or "").strip().lower()
            media_type = "text/markdown" if language in ("markdown", "md") else "text/plain"
            return SourceMetadata(
                state="ok", kind=self.kind,
                canonical_ref=f"document:{doc.id}",
                label=(doc.title or "Untitled"),
                media_type=media_type,
                byte_size=len(body.encode("utf-8", "replace")),
                owner=doc.owner or "",
                revision=f"document:{doc.id}:v{int(doc.version_count or 0)}:{self._digest(body)}",
                version=int(doc.version_count or 0),
                extra={"archived": bool(doc.archived), "language": language,
                       "session_id": doc.session_id or ""},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("document resolver failed for %r: %s", doc_id, exc)
            return self._denied("missing", "the document could not be read")
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def revision(self, ref: SourceRef, *, version_policy: str = "latest",
                 pinned_version: Optional[int] = None) -> str:
        doc_id = (getattr(ref, "id", "") or "").strip()
        if not doc_id:
            return ""
        try:
            db = self._session()
        except Exception:  # noqa: BLE001
            return ""
        try:
            Document, _ = self._models()
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc is None:
                return ""
            content, version, state = self._effective(doc, version_policy, pinned_version, db)
            if state != "ok" or content is None:
                return ""
            return f"document:{doc.id}:v{version}:{self._digest(content)}"
        except Exception as exc:  # noqa: BLE001
            logger.debug("document revision failed for %r: %s", doc_id, exc)
            return ""
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _content(self, ref: SourceRef, version_policy: str,
                 pinned_version: Optional[int]) -> "Tuple[str, str, int, str]":
        """``(content, revision, version, state)`` with no owner check — the
        service authorises through ``validate()`` before it ever gets here."""
        doc_id = (getattr(ref, "id", "") or "").strip()
        if not doc_id:
            return "", "", 0, "missing"
        try:
            db = self._session()
        except Exception:  # noqa: BLE001
            return "", "", 0, "missing"
        try:
            Document, _ = self._models()
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc is None:
                return "", "", 0, "missing"
            content, version, state = self._effective(doc, version_policy, pinned_version, db)
            if state != "ok" or content is None:
                return "", "", 0, state
            return (content, f"document:{doc.id}:v{version}:{self._digest(content)}",
                    version, "ok")
        except Exception as exc:  # noqa: BLE001
            logger.debug("document read failed for %r: %s", doc_id, exc)
            return "", "", 0, "missing"
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def read(self, ref: SourceRef, *, start: int = 0, limit: int = 0,
             version_policy: str = "latest",
             pinned_version: Optional[int] = None) -> SourceContent:
        body, revision, _version, state = self._content(ref, version_policy, pinned_version)
        if state != "ok":
            return SourceContent()
        window, begin, end, total, truncated = self._window(body, start, limit)
        return SourceContent(text=window, start=begin, end=end, total=total,
                             truncated=truncated, revision=revision,
                             media_type="text/markdown")

    def search(self, ref: SourceRef, query: str, *, limit: int = 20) -> List[SourceMatch]:
        body, revision, version, state = self._content(ref, "latest", None)
        if state != "ok":
            return []
        hits = self._matches(body, query, limit,
                             lambda line: {"version": version, "line": line})
        return [SourceMatch(location=h.location, snippet=h.snippet, score=h.score,
                            revision=revision) for h in hits]

    def extract(self, ref: SourceRef, *, version_policy: str = "latest",
                pinned_version: Optional[int] = None) -> ExtractedCorpus:
        body, revision, version, state = self._content(ref, version_policy, pinned_version)
        if state == "unsupported":
            return ExtractedCorpus(degraded=True, note=_SNAPSHOT_NOTE)
        if state != "ok":
            return ExtractedCorpus(degraded=True, note="the document could not be read")
        chunks = tuple(
            ExtractedChunk(index=i, text=body_text, title=title,
                           location={"version": version, "chunk": i},
                           media_type="text/markdown")
            for i, (title, body_text) in enumerate(chunk_markdown(body))
        )
        return ExtractedCorpus(revision=revision, chunks=chunks)
