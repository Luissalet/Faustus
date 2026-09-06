"""
project_context/resolvers/gallery.py — gallery images as project sources.

**Transitional.** ``GalleryImage`` is the old world; ``ArtifactRow`` is the new
one, and ``artifacts.legacy_gallery_id`` is the bridge already built for the
migration. This resolver exists so that a visual reference a user linked today
does not stop working while that migration lands, and it should be deleted the
day the gallery is backfilled into artifacts — at which point ``gallery_image``
links become ``artifact`` links and this file has no reason to exist.

The consequence worth stating: **``GalleryImage`` has no ``project_id``
column.** There is nothing on the row to compare a project against, so this
resolver validates ``owner`` only. The link itself is what supplies membership
— the project's link list is the record that this image belongs to this
project, and it is the only such record. That is exactly why the type is
transitional: an ownership check with no project check is weaker than what the
artifact resolver can do.

Obligations of every resolver (see ``base.py``): owner checked before the
source is touched; ``missing``/``forbidden`` leak nothing; stable revision;
bounded reads; a stored label is never access control.

Images are described, never decoded. The text is the prompt, the caption, the
tags, the model and the dimensions — enough for keyword and semantic retrieval
to find the picture, and no pixels in a textual prompt.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional, Tuple

from ..models import (
    ExtractedChunk, ExtractedCorpus, SourceContent, SourceMatch, SourceMetadata, SourceRef,
)
from .base import ResolverBase

logger = logging.getLogger(__name__)

__all__ = ["GalleryImageResolver"]


class GalleryImageResolver(ResolverBase):
    """Resolver for ``kind='gallery_image'``."""

    kind = "gallery_image"

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from core.database import SessionLocal
        return SessionLocal()

    @staticmethod
    def _model():
        from core.database import GalleryImage
        return GalleryImage

    def _owned(self, db, image_id: str, owner: str) -> "Tuple[Any, str, str]":
        """Owner decided before any row content is read. There is no project
        column to check — see the module docstring."""
        GalleryImage = self._model()
        row = db.query(GalleryImage).filter(GalleryImage.id == str(image_id or "")).first()
        if row is None:
            return None, "missing", "not found"
        if not owner or (row.owner or "") != owner:
            return None, "forbidden", "not owned by the caller"
        return row, "ok", ""

    @staticmethod
    def _revision_of(row) -> str:
        digest = (row.file_hash or "").strip().lower()
        if digest:
            return f"gallery_image:{row.id}:sha256:{digest}"
        return f"gallery_image:{row.id}:{row.filename or ''}"

    @staticmethod
    def _describe(row) -> str:
        lines = [f"# {row.caption or row.filename or row.id}"]
        fields = [
            ("prompt", row.prompt), ("caption", row.caption), ("model", row.model),
            ("size", row.size), ("quality", row.quality), ("tags", row.tags),
            ("ai_tags", row.ai_tags), ("camera", row.camera_model),
            ("width", row.width), ("height", row.height),
        ]
        for name, value in fields:
            cleaned = str(value or "").strip()
            if cleaned:
                lines.append(f"{name}: {cleaned[:2000]}")
        return "\n".join(lines)

    # ── the protocol ───────────────────────────────────────────────────────

    def validate(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        return self.metadata(ref, owner=owner, project=project)

    def metadata(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        image_id = (getattr(ref, "id", "") or "").strip()
        if not image_id:
            return self._denied("missing", "no image id was given")
        try:
            db = self._session()
        except Exception as exc:  # noqa: BLE001 - retrieval path, never raises
            logger.warning("gallery resolver could not open a session: %s", exc)
            return self._denied("missing", "the gallery is unavailable")
        try:
            row, state, note = self._owned(db, image_id, owner)
            if row is None:
                return self._denied(state, note)
            return SourceMetadata(
                state="ok", kind=self.kind, canonical_ref=f"gallery_image:{row.id}",
                label=(row.caption or row.filename or row.id),
                media_type="image/*", byte_size=0, owner=row.owner or "",
                revision=self._revision_of(row), version=0,
                extra={"width": int(row.width or 0), "height": int(row.height or 0),
                       "binary": True, "transitional": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gallery resolver failed for %r: %s", image_id, exc)
            return self._denied("missing", "the image could not be read")
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _load(self, ref: SourceRef):
        image_id = (getattr(ref, "id", "") or "").strip()
        if not image_id:
            return None, None
        try:
            db = self._session()
        except Exception:  # noqa: BLE001
            return None, None
        try:
            GalleryImage = self._model()
            return db.query(GalleryImage).filter(GalleryImage.id == image_id).first(), db
        except Exception as exc:  # noqa: BLE001
            logger.debug("gallery load failed for %r: %s", image_id, exc)
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
        """The image's description. Never the image."""
        row, db = self._load(ref)
        if row is None:
            return SourceContent()
        try:
            body = self._describe(row)
            window, begin, end, total, truncated = self._window(body, start, limit)
            return SourceContent(text=window, start=begin, end=end, total=total,
                                 truncated=truncated, revision=self._revision_of(row),
                                 media_type="text/markdown")
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
            revision = self._revision_of(row)
            hits = self._matches(self._describe(row), query, limit,
                                 lambda line: {"image_id": row.id, "line": line})
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
            return ExtractedCorpus(degraded=True, note="the image could not be read")
        try:
            body = self._describe(row)
            return ExtractedCorpus(
                revision=self._revision_of(row),
                chunks=(ExtractedChunk(index=0, text=body,
                                       title=(row.caption or row.filename or ""),
                                       location={"image_id": row.id, "chunk": 0},
                                       media_type="text/markdown"),),
                degraded=True,
                note="an image is indexed by its description, prompt and tags only",
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
