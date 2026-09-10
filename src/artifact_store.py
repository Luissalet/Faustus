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

Logical ids identify production events, not bytes. `persist()` records each
event through `src/artifact_identity.py`; only the blob is shared. Historical
ids survive the additive copy in `src/artifact_migration.py` as explicit aliases.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
import uuid
import json
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


def _mark_xlsx_formulas_for_recalculation(path: str) -> bool:
    """Best-effort fix for a freshly produced workbook, applied here — before
    it is hashed and published — and nowhere else.

    A formula cell without `fullCalcOnLoad` set shows a viewer's last cached
    value instead of recomputing it on open (QA-39). Mutating a *published*
    blob to fix this would be wrong: blobs are immutable and content-addressed,
    so changing one after publication silently invalidates its own hash. This
    runs only on the run's own file in `source_dir`, before `collect()` ever
    reads or records a hash for it, which is the one place a rewrite is free.

    Returns whether anything was changed; never raises — a workbook this
    cannot open is `collect()`'s problem, not this helper's, and it is caught
    unchanged a few lines later by the normal hash/publish path."""
    if not path.lower().endswith(".xlsx"):
        return False
    try:
        import openpyxl
        from openpyxl.workbook.properties import CalcProperties
    except ImportError:
        return False
    try:
        workbook = openpyxl.load_workbook(path, data_only=False)
        has_formula = any(cell.data_type == "f" for sheet in workbook.worksheets
                          for row in sheet.iter_rows() for cell in row)
        if not has_formula:
            return False
        if workbook.calculation and getattr(workbook.calculation, "fullCalcOnLoad", False):
            return False
        workbook.calculation = CalcProperties(fullCalcOnLoad=True)
        workbook.save(path)
        return True
    except Exception:
        logger.debug("could not inspect/mark xlsx formulas for recalculation on %s",
                     path, exc_info=True)
        return False


def _stored_name(digest: str, original: str) -> str:
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    ext = "".join(c for c in ext if c.isalnum())[:12]
    return f"{digest}.{ext}" if ext else digest


def publish_copy(source: str, store: str, original: str, *, max_bytes=2 * 1024**3):
    """Snapshot, hash and atomically publish bytes without replacing a blob.

    The temporary lives on the destination volume. Readers never observe a
    partially copied target, including when source and store are on different
    drives. An existing target is verified, not blindly trusted by its name.
    """
    os.makedirs(store, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    temporary = None
    try:
        with open(source, 'rb') as incoming, tempfile.NamedTemporaryFile(
                dir=store, prefix='.collect-', delete=False) as outgoing:
            temporary = outgoing.name
            for chunk in iter(lambda: incoming.read(_HASH_CHUNK), b''):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('artifact exceeds the collection byte limit')
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        hexdigest = digest.hexdigest()
        filename = _stored_name(hexdigest, original)
        target = path_of(filename, store_dir=store)
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            if os.path.getsize(target) != size or sha256_of(target) != hexdigest:
                raise ValueError('existing artifact bytes do not match their content hash')
            created = False
        return hexdigest, size, filename, created
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


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
        if '/' in name or '\\' in name or ':' in name or name in ('.', '..'):
            skipped.append({"name": name, "reason": "not_a_bare_name"})
            continue
        if not os.path.isfile(src):
            # The backend saw it and it is gone: a cleanup raced us, or the
            # run deleted its own output. Either way, say so.
            skipped.append({"name": name, "reason": "vanished_before_collection"})
            continue
        if os.path.islink(src) or os.path.commonpath([os.path.realpath(source_dir), os.path.realpath(src)]) != os.path.realpath(source_dir):
            skipped.append({'name': name, 'reason': 'source_is_not_a_regular_workspace_file'})
            continue
        # Still the run's own uncommitted file at this point, never a
        # published blob: safe to rewrite in place before it is hashed.
        _mark_xlsx_formulas_for_recalculation(src)
        before = os.stat(src)
        size = os.path.getsize(src)
        if size > max_bytes:
            skipped.append({"name": name, "reason": f"larger_than_{max_bytes}_bytes"})
            continue

        digest, size, stored, created = publish_copy(src, store, name, max_bytes=max_bytes)
        if not created:
            deduped += 1
        try:
            after = os.stat(src)
            # Don't discard a newer output written while we copied this one.
            if (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns):
                os.unlink(src)
        except OSError:
            logger.debug('could not remove the collected source file', exc_info=True)

        effective_provenance = {'backend': result.backend, **(provenance or {})}
        event = json.dumps([owner, project_id, result.run_id, name, digest,
                            skill_id, skill_version, effective_provenance, retention or {},
                            bool(result.partial)], sort_keys=True, ensure_ascii=False)

        made.append(Artifact.parse({
            "id": 'occ_' + uuid.uuid5(uuid.NAMESPACE_URL, event).hex,
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
            "provenance": effective_provenance,
            "retention": retention or {"policy": "keep"},
        }))
    return Collected(tuple(made), tuple(skipped), deduped)


def persist(artifacts: Iterable[Artifact], *, session_id: str = "",
           call_id: str = "") -> Dict[str, int]:
    """Persist occurrences while deduplicating only the physical bytes.

    Historical rows are copied in background and remain readable meanwhile.
    The historical table remains intact. A repeated event
    is idempotent; reusing its id with different ownership is an error.

    `call_id` is additive and optional: when a caller has one (the tool-call
    id that produced this artifact), it is folded into `provenance.note` so
    an artifact can be traced back to the exact call that made it, without
    widening the `Provenance` contract itself (`note` already exists for
    free-form text like this). Omitting it reproduces the exact provenance
    dict every existing caller already gets.
    """
    from src import artifact_identity as identity
    from src.contracts.blob import ArtifactOccurrence, DerivedArtifact

    created = existing = 0
    for art in artifacts:
        if not art.sha256 or art.byte_size is None:
            raise ValueError('an artifact needs measured bytes before persistence')
        identity.ensure_blob(sha256=art.sha256, byte_size=art.byte_size,
                             filename=art.filename, media_type=art.media_type)
        if art.preview_filename:
            preview = path_of(art.preview_filename)
            identity.record_derivative(DerivedArtifact.parse({
                'source_sha256': art.sha256, 'derived_kind': 'preview',
                'filename': art.preview_filename, 'sha256': sha256_of(preview),
                'byte_size': os.path.getsize(preview),
            }))
        art_provenance = art.provenance.to_dict()
        if call_id:
            note = str(art_provenance.get('note') or '')
            tag = f"call_id={call_id}"
            art_provenance = {**art_provenance,
                              'note': f"{note} {tag}".strip() if note else tag}
        occurrence = ArtifactOccurrence.parse({
            'id': art.id, 'blob_sha256': art.sha256, 'kind': art.kind,
            'label': art.label, 'owner': art.owner, 'project_id': art.project_id,
            'run_id': art.run_id, 'session_id': session_id,
            'skill_id': art.skill_id, 'skill_version': art.skill_version,
            'created_at': art.created_at, 'partial': art.partial,
            'provenance': art_provenance, 'retention': art.retention.to_dict(),
        })
        _, made = identity.ensure_occurrence(occurrence)
        created += int(made)
        existing += int(not made)
        if made:
            _record_manifest_for(occurrence.id, art, call_id=call_id)
    return {'created': created, 'already_there': existing}


def _record_manifest_for(occurrence_id: str, art: Artifact, *, call_id: str = "") -> None:
    """Give a freshly recorded occurrence its first manifest version (ART-01).

    Runs a format validation when the artifact's bytes are still on disk and
    the format has a check (`identity.validate_artifact_bytes()`); when every
    check passes the manifest moves straight to `validated`, and when one
    fails it stays at `generated` — the state distinguishes "the run produced
    this" from "this was checked and is fine" without the caller asking twice.

    Best-effort and never fatal: the occurrence itself is already committed by
    the time this runs, and a manifest bookkeeping failure must not undo that.
    """
    from src import artifact_identity as identity

    try:
        validations = None
        try:
            file_path = path_of(art.filename)
        except ValueError:
            file_path = ""
        if file_path and os.path.isfile(file_path):
            result = identity.validate_artifact_bytes(
                file_path, kind=art.kind, media_type=art.media_type,
                filename=art.label or art.filename)
            validations = result['checks']
        identity.record_manifest_version(
            occurrence_id, state='generated', call_id=call_id,
            validations=validations, format=art.media_type or art.kind,
            byte_size=art.byte_size, sha256=art.sha256,
            generator=art.provenance.backend or art.skill_id or '')
        ran_a_real_check = validations is not None and any(
            check['name'] != 'no_validator' for check in validations)
        if ran_a_real_check and all(check['ok'] for check in validations):
            identity.transition_state(
                occurrence_id, 'validated', call_id=call_id,
                reason='automatic format validation passed on collection')
    except Exception:
        logger.exception('artifact manifest recording failed for %s; the '
                         'occurrence itself was already recorded', occurrence_id)


def analysis_provenance(*, script_text: str, input_paths: Iterable[str] = (),
                        engine: str = "", recipe: str = "",
                        source_artifact_ids: Iterable[str] = ()) -> Dict[str, Any]:
    """ART-05 (datos y análisis reproducibles): the provenance dict for a
    saved script + its input data, so `collect(..., provenance=...)` records
    "this number comes from this exact script and this exact data" as
    something checkable, not a caption.

    Deliberately reuses the `Provenance` contract's existing fields
    (src/contracts/artifact.py) rather than inventing a parallel schema for
    analyses (COMUN.md rule 4): `recipe_fingerprint` already means "the
    recipe file had not been edited underneath that name" — that is exactly
    what hashing the analysis script means here — and `inputs_digest`
    already means "fingerprint of inputs, never the inputs", which is
    exactly "the data this used, hashed, not copied". `source_artifact_ids`
    lets the input data itself be a prior artifact, making the chain walkable
    the same way any other artifact lineage is.

    Sandboxed execution, read-only DB access and timeouts (ART-05's other
    half) belong to EXEC-01's authority, not this module; this function only
    records what ran, once EXEC-01 (or any other reproducible-analysis
    caller) has actually run it.
    """
    script_hash = hashlib.sha256((script_text or "").encode("utf-8")).hexdigest()
    input_hashes = []
    unreadable = []
    for p in input_paths:
        try:
            input_hashes.append(sha256_of(p))
        except OSError:
            unreadable.append(p)
    inputs_digest = None
    if input_hashes:
        inputs_digest = hashlib.sha256(
            "\n".join(sorted(input_hashes)).encode("utf-8")).hexdigest()
    note = f"reproducible analysis: script sha256={script_hash}"
    if inputs_digest:
        note += f"; inputs sha256={inputs_digest} ({len(input_hashes)} file(s))"
    if unreadable:
        note += f"; {len(unreadable)} input path(s) unreadable at record time"
    return {
        "backend": engine or None,
        "recipe": recipe or None,
        "recipe_fingerprint": script_hash,
        "inputs_digest": inputs_digest,
        "source_artifact_ids": tuple(source_artifact_ids),
        "note": note,
    }


def path_of(artifact_filename: str, *, store_dir: Optional[str] = None) -> str:
    """Resolve a stored name to a path, refusing anything that is not a bare
    name inside the store. The contract already rejects a path in `filename`;
    this is the second lock, on the side that touches the filesystem."""
    store = os.path.realpath(store_dir or ARTIFACT_STORE_DIR)
    if not artifact_filename or '\\' in artifact_filename or '/' in artifact_filename or ':' in artifact_filename or artifact_filename in ('.', '..'):
        raise ValueError(f"{artifact_filename!r} is not a bare artifact name")
    resolved = os.path.realpath(os.path.join(store, artifact_filename))
    if os.path.commonpath([store, resolved]) != store:
        raise ValueError(f"{artifact_filename!r} resolves outside the artifact store")
    return resolved
