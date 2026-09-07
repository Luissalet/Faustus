"""Whether the files a run produced are still there, and still themselves.

`src/artifact_catalog.py` lists occurrences and blobs, plus historical rows
not yet copied. Scope and logical identity survive physical deduplication.

Two fields come off the filesystem and are therefore `observed`: `exists` and
`byte_size`. They are the ones worth having. A row in the artifacts table is
not evidence that a file is on disk -- a store directory that moved, a cleanup
that ran, a volume that did not mount all produce a table full of rows about
files that are gone, and a run that reports success while its output has
vanished is precisely the lie this subsystem exists to catch.

`byte_size` is only set when the file exists. Zero is a real size (an empty
file) and `unknown` is a real answer, so writing `0` for a missing file would
collapse the two.

**Integrity is compared, never recomputed.** The store is content-addressed:
`artifact_store._stored_name` builds the filename out of the sha256, so the
name IS a recorded digest and comparing it against the row's `sha256` column
costs no I/O at all. That is the whole check. Re-hashing the files on a sweep
would read every byte the machine has ever produced, on a timer, to answer a
question almost always answered `ok` -- and the field is absent rather than
guessed when either half is missing, which the reducer reads as "not sampled".

Entity ids: `artifact://<owner>/<namespace>/<artifact_id>`. `source_refs` carry
`artifact:<artifact_id>`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

SCHEMA = "artifact_state.v1"

#: What `integrity` may say. Two words, because those are the two answers the
#: free comparison can give; anything it could not check leaves the field
#: absent rather than growing a third word meaning "we did not look".
INTEGRITY_OK = "ok"
INTEGRITY_MISMATCH = "mismatch"
INTEGRITY_STATES: Tuple[str, ...] = (INTEGRITY_OK, INTEGRITY_MISMATCH)

__all__ = ["ArtifactsAdapter", "INTEGRITY_OK", "INTEGRITY_MISMATCH",
           "INTEGRITY_STATES", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _recorded_digest(filename: str) -> str:
    """The sha256 the stored name carries, or `""` when it carries none.

    `_stored_name` writes `<digest>.<ext>` or a bare `<digest>`. Anything that
    is not 64 hex characters was not minted by this store -- a legacy row, an
    import from the gallery -- and has no recorded digest to compare against.
    """
    stem = str(filename or "").split(".", 1)[0].strip().lower()
    if len(stem) != 64:
        return ""
    return stem if all(c in "0123456789abcdef" for c in stem) else ""


def _integrity(row: Any) -> Optional[str]:
    """`ok`, `mismatch`, or `None` for "nothing to compare".

    Both halves have to be recorded. A row with no `sha256` and a name that is
    not a digest is not a corrupt artifact; it is an artifact nobody hashed,
    and saying `ok` about it would be the most expensive kind of wrong.
    """
    claimed = _word(getattr(row, "sha256", ""), 64).lower()
    stored = _recorded_digest(_word(getattr(row, "filename", ""), 512))
    if not claimed or not stored:
        return None
    return INTEGRITY_OK if claimed == stored else INTEGRITY_MISMATCH


class ArtifactsAdapter(ThreadedAdapter):
    """Run outputs: are they on disk, how big, and do they still match."""

    name = "artifacts"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        try:
            import src.artifact_store                          # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("artifacts adapter: src.artifact_store unavailable: %s", exc)
            return False
        return True

    def _rows(self, scope: Scope) -> List[Any]:
        """The newest artifacts for this owner.

        Newest first and capped, because the store is append-only and grows
        with every run: a sweep that walked all of it would stat the entire
        history of the machine to learn about the last hour of it.
        """
        from core.database import SessionLocal
        from src.artifact_catalog import recent

        db = SessionLocal()
        try:
            return recent(db, owner=scope.owner, project_id=scope.project_id,
                          limit=scope.capped(200))
        finally:
            db.close()

    def _on_disk(self, row: Any) -> Tuple[Optional[bool], Optional[int]]:
        """`(exists, byte_size)` from the filesystem, or `(None, None)`.

        `(None, None)` when the name cannot be resolved to a path at all --
        `path_of` refuses anything that is not a bare name inside the store --
        because "this row's filename is not a filename" is not the same fact as
        "the file is gone", and only the second one means somebody lost work.
        """
        from src.artifact_catalog import path as catalog_path
        path = self._safe(catalog_path, row, default="")
        if not path:
            return None, None
        if not os.path.isfile(path):
            return False, None
        return True, self._safe(os.path.getsize, path)

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for row in (self._safe(self._rows, scope, default=[]) or []):
            ident = _word(getattr(row, "id", ""), 128)
            if not ident:
                continue
            made = entity("artifact", ident, scope=scope, schema=SCHEMA,
                          display_name=_word(getattr(row, "label", "")
                                             or getattr(row, "filename", ""), 300),
                          labels=(_word(getattr(row, "kind", ""), 32),),
                          source_refs=(f"artifact:{ident}",),
                          created_at=getattr(row, "created_at", ""))
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        stamp = now_iso()
        out: List[StateObservation] = []
        for row in (self._safe(self._rows, scope, default=[]) or []):
            ident = _word(getattr(row, "id", ""), 128)
            if not ident:
                continue
            target = self._safe(entity_id, "artifact", scope.owner, ident,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            exists, size = self._safe(self._on_disk, row, default=(None, None))
            observed: Dict[str, Any] = {
                "exists": exists,
                "byte_size": size,
                "kind": _word(getattr(row, "kind", ""), 32) or None,
            }
            # A comparison of two recorded values, reproducible from the row
            # alone. Nothing was measured, so it is not `observed`.
            derived: Dict[str, Any] = {"integrity": _integrity(row)}
            for epistemic, body in (("observed", observed), ("derived", derived)):
                made = observation(target, self.name, body, scope=scope,
                                   schema=SCHEMA, epistemic=epistemic,
                                   observed_at=stamp,
                                   evidence_refs=(f"artifact:{ident}",))
                if made is not None:
                    out.append(made)
        return out
