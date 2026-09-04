"""
artifact_store.py — what the run left behind, kept with its provenance.

"Todo output es un artefacto."  This is the module that makes that true for
the sandbox: it takes the files a run wrote into its own `/artifacts`
directory, hashes them, types them, gives them a home named by content, and
records where they came from.

Four decisions worth stating, because each has a tempting wrong version:

* **Named by content hash.** Two runs that produce identical bytes share one
  file, and re-running a deterministic skill does not double the disk. The
  filename is `<sha256>.<ext>`, so the name cannot collide with a name the run
  chose and a run cannot overwrite an earlier artifact by picking its name.
  The name the run used survives in `label`.
* **A type it cannot infer is `binary`, not a guess.** The alternative to an
  honest bucket is either dropping the user's output or writing a kind into an
  audit table that nothing verified.
* **A partial run's outputs are kept and marked partial.** A render killed at
  90% has produced something, and deleting it to keep the table tidy throws
  away the only evidence of what went wrong.
* **Nothing here decides policy.** It records `backend`, `run_id` and whatever
  provenance the caller can prove, and leaves every field it cannot know as
  NULL for `Artifact.provenance_gaps()` to report.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.constants import ARTIFACT_RUNS_DIR, ARTIFACT_STORE_DIR
from src.contracts import Artifact, ExecutionResult
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: Extension → kind. Deliberately explicit: a table someone can read and
#: correct beats a heuristic nobody can audit.
_KIND_BY_EXT = {
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image",
    "gif": "image", "bmp": "image", "tif": "image", "tiff": "image", "svg": "image",
    "mp4": "video", "mov": "video", "webm": "video", "mkv": "video", "m4v": "video",
    "avi": "video",
    "mp3": "audio", "wav": "audio", "flac": "audio", "ogg": "audio",
    "m4a": "audio", "opus": "audio",
    "pdf": "document", "docx": "document", "odt": "document", "epub": "document",
    "pptx": "document", "rtf": "document",
    "md": "document", "markdown": "document",
    "txt": "text", "log": "text",
    "json": "json", "jsonl": "json",
    "csv": "dataset", "tsv": "dataset", "parquet": "dataset", "xlsx": "dataset",
    "zip": "archive", "tar": "archive", "gz": "archive", "tgz": "archive",
    "7z": "archive", "bz2": "archive", "xz": "archive",
    "py": "code", "js": "code", "ts": "code", "tsx": "code", "jsx": "code",
    "sh": "code", "ps1": "code", "rs": "code", "go": "code", "java": "code",
    "c": "code", "h": "code", "cpp": "code", "hpp": "code", "sql": "code",
    "html": "code", "css": "code", "yaml": "code", "yml": "code", "toml": "code",
    "patch": "code", "diff": "code",
}

#: Nobody's output is worth reading 4 GB into memory to hash in one go.
_HASH_CHUNK = 1024 * 1024


def run_slug(run_id: str) -> str:
    """The directory name for a run id, without touching the disk."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in (run_id or "run"))[:64]


def run_dir(run_id: str, *, root: Optional[str] = None) -> str:
    """The scratch directory a single run writes into. One per run, so
    `/artifacts` really is empty when the container starts."""
    path = os.path.join(root or ARTIFACT_RUNS_DIR, run_slug(run_id))
    os.makedirs(path, exist_ok=True)
    return path


def kind_of(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _KIND_BY_EXT.get(ext, "binary")


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stored_name(digest: str, original: str) -> str:
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    ext = "".join(c for c in ext if c.isalnum())[:12]
    return f"{digest}.{ext}" if ext else digest


@dataclass(frozen=True)
class Collected:
    """What `collect()` did, including what it would not touch."""

    artifacts: Tuple[Artifact, ...] = ()
    skipped: Tuple[Dict[str, str], ...] = ()
    deduplicated: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"artifacts": [a.to_dict() for a in self.artifacts],
                "skipped": [dict(s) for s in self.skipped],
                "deduplicated": self.deduplicated}


def collect(result: ExecutionResult, *, source_dir: str,
            owner: str = "", project_id: str = "",
            skill_id: str = "", skill_version: str = "",
            provenance: Optional[Dict[str, Any]] = None,
            retention: Optional[Dict[str, Any]] = None,
            store_dir: Optional[str] = None,
            max_bytes: int = 2 * 1024 * 1024 * 1024) -> Collected:
    """Move what this run produced into the store and describe each piece.

    Only the filenames the *result* names are considered — the backend already
    worked out which files this run wrote, and re-listing the directory here
    would re-introduce the bug where a run is credited with a neighbour's
    output."""
    store = store_dir or ARTIFACT_STORE_DIR
    os.makedirs(store, exist_ok=True)
    made: List[Artifact] = []
    skipped: List[Dict[str, str]] = []
    deduped = 0

    for name in result.artifact_filenames:
        src = os.path.join(source_dir, name)
        if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
            skipped.append({"name": name, "reason": "not_a_bare_name"})
            continue
        if not os.path.isfile(src):
            # The backend saw it and it is gone: a cleanup raced us, or the
            # run deleted its own output. Either way, say so.
            skipped.append({"name": name, "reason": "vanished_before_collection"})
            continue
        size = os.path.getsize(src)
        if size > max_bytes:
            skipped.append({"name": name, "reason": f"larger_than_{max_bytes}_bytes"})
            continue

        digest = sha256_of(src)
        stored = _stored_name(digest, name)
        target = os.path.join(store, stored)
        if os.path.exists(target):
            # Same bytes, same name. Nothing to write, and the earlier file is
            # not replaced — identical content is identical content.
            deduped += 1
            try:
                os.unlink(src)
            except OSError:
                logger.debug("could not remove the collected source file", exc_info=True)
        else:
            shutil.move(src, target)

        made.append(Artifact.parse({
            "id": f"art_{digest[:24]}",
            "kind": kind_of(name),
            "filename": stored,
            "sha256": digest,
            "media_type": mimetypes.guess_type(name)[0] or "",
            "byte_size": size,
            "label": name,
            "owner": owner, "project_id": project_id,
            "run_id": result.run_id,
            "skill_id": skill_id, "skill_version": skill_version,
            "created_at": now_iso(),
            "partial": bool(result.partial),
            "provenance": {"backend": result.backend, **(provenance or {})},
            "retention": retention or {"policy": "keep"},
        }))
    return Collected(tuple(made), tuple(skipped), deduped)


def persist(artifacts: Iterable[Artifact], *, session_id: str = "") -> Dict[str, int]:
    """Write the rows. Idempotent by artifact id, which is derived from the
    content hash, so collecting the same bytes twice updates nothing."""
    from core.database import ArtifactRow, SessionLocal

    created = existing = 0
    db = SessionLocal()
    try:
        for art in artifacts:
            if db.get(ArtifactRow, art.id) is not None:
                existing += 1
                continue
            p = art.provenance
            db.add(ArtifactRow(
                id=art.id, kind=art.kind, filename=art.filename,
                sha256=art.sha256 or None, media_type=art.media_type or None,
                byte_size=art.byte_size, label=art.label, partial=art.partial,
                preview_filename=art.preview_filename or None,
                owner=art.owner or None, project_id=art.project_id or None,
                run_id=art.run_id or None, session_id=session_id or None,
                skill_id=art.skill_id or None, skill_version=art.skill_version or None,
                model=p.model, model_license=p.model_license, backend=p.backend,
                recipe=p.recipe, recipe_version=p.recipe_version,
                inputs_digest=p.inputs_digest, provenance_note=p.note or "",
                retention_policy=art.retention.policy,
                retention_days=art.retention.days,
                retention_reason=art.retention.reason or "",
                schema_version=art.schema_version,
            ))
            created += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"created": created, "already_there": existing}


def path_of(artifact_filename: str, *, store_dir: Optional[str] = None) -> str:
    """Resolve a stored name to a path, refusing anything that is not a bare
    name inside the store. The contract already rejects a path in `filename`;
    this is the second lock, on the side that touches the filesystem."""
    store = os.path.abspath(store_dir or ARTIFACT_STORE_DIR)
    if not artifact_filename or os.path.sep in artifact_filename or "/" in artifact_filename:
        raise ValueError(f"{artifact_filename!r} is not a bare artifact name")
    resolved = os.path.abspath(os.path.join(store, artifact_filename))
    if os.path.commonpath([store, resolved]) != store:
        raise ValueError(f"{artifact_filename!r} resolves outside the artifact store")
    return resolved
