"""
project_context/resolvers/artifact.py — run outputs as project sources.

Reads occurrences and their blobs through ``src.artifact_catalog``. Legacy
aliases and unmigrated rows remain readable during the additive copy. Stored
paths are confined to the artifact store (or the historical gallery root).

Obligations of every resolver (see ``base.py``): owner checked before the
source is touched; ``missing``/``forbidden`` leak nothing; stable revision;
bounded reads; a stored label is never access control.

Project compatibility
---------------------
An artifact carries its own ``project_id``. When it is set and disagrees with
the project doing the linking, the answer is ``forbidden`` — an output of
project A does not become knowledge of project B because a model asked
politely. When it is NULL the artifact predates project attribution (or came
from the gallery backfill), and linking is allowed: NULL means unknown, never
"belongs to everyone".

What is extracted, by kind
--------------------------
* Markdown, text, JSON, CSV and code are read directly.
* PDF goes through ``src.personal_docs.extract_pdf_text`` (pypdf), DOCX and the
  other Office formats through ``extract_office_text``, which routes to the
  **optional** markitdown dependency. When markitdown is absent the corpus
  comes back ``degraded=True`` with a note. It does not raise: an optional
  dependency that can break an attach is not optional.
* Image, video and audio are described, never decoded. The text is the label,
  the media type, the size and the generation recipe — model, backend, seed,
  recipe and version. The bytes themselves never enter a textual prompt; which
  representation a multimodal model should get is the Context Engine's call,
  not this resolver's.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Mapping, Optional, Tuple

from ..models import (
    ExtractedChunk, ExtractedCorpus, SourceContent, SourceMatch, SourceMetadata, SourceRef,
)
from .base import MAX_READ_CHARS, ResolverBase

logger = logging.getLogger(__name__)

__all__ = ["ArtifactResolver"]

#: Kinds whose bytes are text and can be read straight off the disk.
_TEXT_KINDS = frozenset({"text", "json", "code", "dataset"})

#: Kinds that are described rather than decoded.
_MEDIA_KINDS = frozenset({"image", "video", "audio"})

_PDF_EXTS = frozenset({".pdf"})
_OFFICE_EXTS = frozenset({".docx", ".pptx", ".xlsx", ".xls", ".epub"})
_PLAIN_EXTS = frozenset({
    ".md", ".markdown", ".txt", ".log", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".toml", ".xml", ".html", ".py", ".js", ".ts", ".sql",
    ".sh", ".ps1", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".css",
})

_MARKITDOWN_NOTE = (
    "Office extraction fell back to the built-in reader: the optional markitdown "
    "dependency is not installed (pip install -r requirements-optional.txt)."
)


class ArtifactResolver(ResolverBase):
    """Resolver for ``kind='artifact'``."""

    kind = "artifact"

    def __init__(self, session_factory=None, *, store_dir: Optional[str] = None) -> None:
        self._session_factory = session_factory
        self._store_dir = store_dir

    # ── plumbing ───────────────────────────────────────────────────────────

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from core.database import SessionLocal
        return SessionLocal()

    @staticmethod
    def _model():
        from core.database import ArtifactRow
        return ArtifactRow

    def _path(self, filename: str) -> str:
        """The artifact's path, through the store's own name guard."""
        from src.artifact_store import path_of
        return path_of(filename, store_dir=self._store_dir)

    def _row(self, db, artifact_id: str):
        from src.artifact_catalog import get
        return get(db, str(artifact_id or ""))

    def _authorised(self, db, artifact_id: str, owner: str,
                    project: Mapping[str, Any]) -> "Tuple[Any, str, str]":
        """``(row, state, note)``. Ownership and project compatibility are both
        decided before any content is read out of the row."""
        row = self._row(db, artifact_id)
        if row is None:
            return None, "missing", "not found"
        if not owner or (row.owner or "") != owner:
            return None, "forbidden", "not owned by the caller"
        row_project = (row.project_id or "").strip()
        want = str((project or {}).get("id") or "").strip()
        if row_project and want and row_project != want:
            return None, "forbidden", "belongs to a different project"
        return row, "ok", ""

    @staticmethod
    def _revision_of(row) -> str:
        if (row.sha256 or "").strip():
            return f"artifact:{row.id}:sha256:{row.sha256.strip().lower()}"
        return f"artifact:{row.id}:{int(row.byte_size or 0)}:{row.filename or ''}"

    def _describe(self, row) -> str:
        """The text stand-in for a binary artifact: what it is and how it was
        made. Never its bytes."""
        lines = [
            f"# {row.label or row.filename or row.id}",
            f"kind: {row.kind}",
            f"media_type: {row.media_type or 'unknown'}",
            f"bytes: {int(row.byte_size or 0)}",
        ]
        recipe = [
            ("model", row.model), ("backend", row.backend), ("recipe", row.recipe),
            ("recipe_version", row.recipe_version), ("engine", row.engine),
            ("seed", row.seed),
        ]
        known = [f"{name}: {value}" for name, value in recipe if value not in (None, "")]
        if known:
            lines.append("")
            lines.append("## generation recipe")
            lines.extend(known)
        if (row.provenance_note or "").strip():
            lines.append("")
            lines.append("## provenance")
            lines.append(row.provenance_note.strip()[:2000])
        return "\n".join(lines)

    def _text_of(self, row) -> "Tuple[str, bool, str]":
        """``(text, degraded, note)`` for one artifact's content."""
        if (row.kind or "") in _MEDIA_KINDS:
            return self._describe(row), False, "binary content is described, not decoded"
        try:
            from src.artifact_catalog import path as catalog_path
            path = catalog_path(row, store_dir=self._store_dir)
        except (ValueError, TypeError) as exc:
            logger.debug("artifact path refused for %r: %s", row.id, exc)
            return "", True, "the artifact filename is not a bare store name"
        if not os.path.isfile(path):
            return "", True, "the artifact file is not in the store"
        ext = os.path.splitext(path)[1].lower()
        if ext in _PDF_EXTS:
            from src.personal_docs import extract_pdf_text
            body = extract_pdf_text(path) or ""
            return body, (not body.strip()), ("" if body.strip() else "no text could be "
                                              "extracted from the PDF")
        if ext in _OFFICE_EXTS:
            from src.markitdown_runtime import MARKITDOWN_EXTS  # noqa: F401 - documents the route
            try:
                from src.personal_docs import extract_office_text
                body = extract_office_text(path) or ""
            except Exception as exc:  # noqa: BLE001 - optional dependency, never fatal
                logger.debug("office extraction failed for %r: %s", row.id, exc)
                body = ""
            if not body.strip():
                return "", True, _MARKITDOWN_NOTE
            return body, False, ""
        if ext in _PLAIN_EXTS or (row.kind or "") in _TEXT_KINDS or (row.kind or "") == "document":
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read(MAX_READ_CHARS + 1), False, ""
            except OSError as exc:
                logger.debug("artifact read failed for %r: %s", row.id, exc)
                return "", True, "the artifact could not be read"
        return self._describe(row), True, "no text extractor for this artifact type"

    # ── the protocol ───────────────────────────────────────────────────────

    def validate(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        return self.metadata(ref, owner=owner, project=project)

    def metadata(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        artifact_id = (getattr(ref, "id", "") or "").strip()
        if not artifact_id:
            return self._denied("missing", "no artifact id was given")
        try:
            db = self._session()
        except Exception as exc:  # noqa: BLE001 - retrieval path, never raises
            logger.warning("artifact resolver could not open a session: %s", exc)
            return self._denied("missing", "the artifact store is unavailable")
        try:
            row, state, note = self._authorised(db, artifact_id, owner, project)
            if row is None:
                return self._denied(state, note)
            return SourceMetadata(
                state="ok", kind=self.kind, canonical_ref=f"artifact:{row.id}",
                label=(row.label or row.filename or row.id),
                media_type=(row.media_type or ""), byte_size=int(row.byte_size or 0),
                owner=row.owner or "", revision=self._revision_of(row), version=0,
                extra={"artifact_kind": row.kind or "", "partial": bool(row.partial),
                       "project_id": row.project_id or "", "run_id": row.run_id or "",
                       "binary": (row.kind or "") in _MEDIA_KINDS},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("artifact resolver failed for %r: %s", artifact_id, exc)
            return self._denied("missing", "the artifact could not be read")
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _load(self, ref: SourceRef):
        """``(row, db)`` with no owner check — the service authorises through
        ``validate()`` first. Caller closes ``db``."""
        artifact_id = (getattr(ref, "id", "") or "").strip()
        if not artifact_id:
            return None, None
        try:
            db = self._session()
        except Exception:  # noqa: BLE001
            return None, None
        try:
            return self._row(db, artifact_id), db
        except Exception as exc:  # noqa: BLE001
            logger.debug("artifact load failed for %r: %s", artifact_id, exc)
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            return None, None

    def revision(self, ref: SourceRef, *, version_policy: str = "latest",
                 pinned_version: Optional[int] = None) -> str:
        row, db = self._load(ref)
        try:
            return self._revision_of(row) if row is not None else ""
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    def read(self, ref: SourceRef, *, start: int = 0, limit: int = 0,
             version_policy: str = "latest",
             pinned_version: Optional[int] = None) -> SourceContent:
        row, db = self._load(ref)
        if row is None:
            return SourceContent()
        try:
            body, _degraded, _note = self._text_of(row)
            window, begin, end, total, truncated = self._window(body, start, limit)
            return SourceContent(text=window, start=begin, end=end, total=total,
                                 truncated=truncated, revision=self._revision_of(row),
                                 media_type=(row.media_type or "text/plain"))
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    def search(self, ref: SourceRef, query: str, *, limit: int = 20) -> List[SourceMatch]:
        row, db = self._load(ref)
        if row is None:
            return []
        try:
            body, _degraded, _note = self._text_of(row)
            revision = self._revision_of(row)
            hits = self._matches(body, query, limit,
                                 lambda line: {"artifact_id": row.id, "line": line})
            return [SourceMatch(location=h.location, snippet=h.snippet, score=h.score,
                                revision=revision) for h in hits]
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    def extract(self, ref: SourceRef, *, version_policy: str = "latest",
                pinned_version: Optional[int] = None) -> ExtractedCorpus:
        row, db = self._load(ref)
        if row is None:
            return ExtractedCorpus(degraded=True, note="the artifact could not be read")
        try:
            body, degraded, note = self._text_of(row)
            revision = self._revision_of(row)
            if not body.strip():
                return ExtractedCorpus(revision=revision, degraded=True,
                                       note=note or "nothing extractable")
            from .document import chunk_markdown
            chunks = tuple(
                ExtractedChunk(index=i, text=part,
                               title=title or (row.label or row.filename or ""),
                               location={"artifact_id": row.id, "chunk": i},
                               media_type=(row.media_type or "text/plain"))
                for i, (title, part) in enumerate(chunk_markdown(body))
            )
            return ExtractedCorpus(revision=revision, chunks=chunks,
                                   degraded=degraded, note=note)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
