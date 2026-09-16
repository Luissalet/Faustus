"""ingest.py — bounded ingestion of local files and URLs into the Creator
library (WP04), and the resumable job ledger that tracks it.

CONTRATO.md rule 1: no parallel authority. Ingested bytes land in
``src.artifact_identity`` exactly like anything ``src.creator.library``
already lists (image/video/audio/document/text occurrences owned by the
authenticated caller); proxies are plain ``ArtifactOccurrence`` rows with
``relations.derived_from`` pointing at the source, the same shape WP03's
``library.generate_proxy`` already uses — this module calls that function
for the image thumbnail / video frame / audio waveform it already knows how
to build, and adds exactly two things WP03 does not: a bounded 480p video
preview and an audio peaks JSON (numpy, when installed), because WP04's
ficha asks for both and neither belongs in WP03's ownership.

Three checks run BEFORE a single byte is written to the durable artifact
store or ``creator/ingest.db`` (``docs/09_SEGURIDAD_LICENCIAS_Y_MIGRACION.md``,
"Ejecución y codecs" — "verificar tipo real, limitar dimensiones..."):

1. size, against ``creator_ingest_max_bytes`` (a cheap ``os.stat``, no read)
2. real type, by sniffing magic bytes (``python-magic`` if installed, else a
   small signature table this module owns) — never trusted from a claimed
   filename or ``Content-Type`` header
3. for video/audio: ``ffprobe`` validates the container (its ``returncode``
   is checked, never just its stdout — the gap
   ``docs/spec/creator/00_WP00_INVENTARIO.md`` §4 names) and its duration
   against ``creator_ingest_max_duration_s``

Only after all three pass is anything written. A job is keyed by
``sha256(owner, project_id, source sha256-or-url)`` (``_job_id``), so
re-ingesting identical bytes/URL is a no-op that returns the existing job
(idempotent by content, not by request) — CONTRATO.md rule 4's dedupe spirit
applied to a job rather than a single command.

Resumability: the job row is the only thing this module trusts across a
restart. The source file itself is caller-owned (an upload's own temp file,
deleted once the request ends) and is NOT assumed to still exist after a
crash, so what this module can actually resume is the step whose inputs are
already durable: once a job reaches ``proxying`` its source occurrence is
already recorded in ``src.artifact_identity`` (real bytes, on disk, owned),
so ``resume_job``/``resume_pending`` re-read that occurrence and finish the
proxy step without ever touching the original upload again. A job stuck
before that point (``pending``/``validating``) has nothing durable yet to
resume FROM; the caller re-submits the same source and gets the same job id
back (idempotent), not a silent retry from a temp file that may be gone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from src.contracts.base import now_iso

from .errors import CreatorError

logger = logging.getLogger(__name__)

#: The only kinds a FILE ingest accepts (CONTRATO.md rule 1: this module
#: does not invent a document/code/dataset ingestion path — that is
#: `routes/upload_routes.py`'s job. WP04 is scoped to media proxies.).
FILE_KINDS = ("image", "video", "audio")

_BUSY_TIMEOUT_S = 30
_JOB_STATES = ("pending", "validating", "proxying", "done", "failed")

#: URL text ingestion is bounded the same way `tool_result_offload`
#: previews a huge tool result — a hard character cap, not a "best effort"
#: truncation the caller has to guess at.
MAX_URL_TEXT_CHARS = 20000

#: Bounded ffmpeg/ffprobe subprocess wall-clock limits (docs/09: "límites de
#: CPU, memoria, disco y tiempo").
_FFPROBE_TIMEOUT_S = 30
_FFMPEG_PROXY_TIMEOUT_S = 180
_FFMPEG_PEAKS_TIMEOUT_S = 60


class IngestValidationError(CreatorError):
    """A source failed a bounded check (size, real type, container,
    duration) before anything was written. The message is safe to show the
    caller verbatim — it never echoes raw ffmpeg/ffprobe stderr."""


class IngestJobNotFound(CreatorError):
    """No job with that id, or it belongs to someone else — 404 either way
    per CONTRATO.md rule 3."""


# ── settings ─────────────────────────────────────────────────────────────

def max_ingest_bytes() -> int:
    from src.settings import get_setting
    try:
        value = int(get_setting("creator_ingest_max_bytes", 300 * 1024 * 1024) or 0)
    except (TypeError, ValueError):
        value = 300 * 1024 * 1024
    return value if value > 0 else 300 * 1024 * 1024


def max_ingest_duration_s() -> float:
    from src.settings import get_setting
    try:
        value = float(get_setting("creator_ingest_max_duration_s", 1800) or 0)
    except (TypeError, ValueError):
        value = 1800.0
    return value if value > 0 else 1800.0


# ── job ledger: DATA_DIR/creator/ingest.db ──────────────────────────────

def default_db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "ingest.db")


_init_lock = threading.Lock()


@contextmanager
def _conn(db_path: str) -> Iterator[sqlite3.Connection]:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        conn.close()


def _ensure_schema(db_path: str) -> None:
    with _init_lock, _conn(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingest_jobs ("
            "id TEXT PRIMARY KEY, owner TEXT NOT NULL, project_id TEXT NOT NULL, "
            "source_kind TEXT NOT NULL, source_label TEXT NOT NULL, "
            "content_key TEXT NOT NULL, state TEXT NOT NULL, error TEXT, "
            "occurrence_id TEXT, proxies_json TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ingest_jobs_owner ON ingest_jobs(owner)")


def _job_id(owner: str, project_id: str, content_key: str) -> str:
    seed = f"{owner}:{project_id}:{content_key}".encode("utf-8")
    return "ing_" + hashlib.sha1(seed).hexdigest()[:24]  # noqa: S324 - id, not a security digest


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    proxies = []
    if row["proxies_json"]:
        try:
            proxies = json.loads(row["proxies_json"])
        except (TypeError, ValueError):
            proxies = []
    return {
        "job_id": row["id"], "owner": row["owner"], "project_id": row["project_id"],
        "source_kind": row["source_kind"], "source_label": row["source_label"],
        "state": row["state"], "error": row["error"] or "",
        "occurrence_id": row["occurrence_id"] or "", "proxies": proxies,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _get_job_row(db_path: str, job_id: str) -> Optional[Dict[str, Any]]:
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None


def _create_job(db_path: str, job_id: str, *, owner: str, project_id: str,
                source_kind: str, source_label: str, content_key: str) -> Dict[str, Any]:
    _ensure_schema(db_path)
    now = time.time()
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ingest_jobs "
            "(id, owner, project_id, source_kind, source_label, content_key, "
            "state, error, occurrence_id, proxies_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, owner, project_id, source_kind, source_label, content_key,
             "pending", None, None, None, now, now))
    return _get_job_row(db_path, job_id)  # type: ignore[return-value]


def _update_job(db_path: str, job_id: str, *, state: str, error: Optional[str] = None,
                occurrence_id: Optional[str] = None,
                proxies: Optional[List[Dict[str, Any]]] = None) -> None:
    assert state in _JOB_STATES, f"unknown ingest job state {state!r}"
    fields = ["state=?", "updated_at=?"]
    values: List[Any] = [state, time.time()]
    fields.append("error=?")
    values.append(error)
    if occurrence_id is not None:
        fields.append("occurrence_id=?")
        values.append(occurrence_id)
    if proxies is not None:
        fields.append("proxies_json=?")
        values.append(json.dumps(proxies))
    values.append(job_id)
    with _conn(db_path) as conn:
        conn.execute(f"UPDATE ingest_jobs SET {', '.join(fields)} WHERE id=?", values)


def get_job(owner: str, job_id: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A job this owner started, or ``None`` — never another owner's job
    (CONTRATO.md rule 3: the route maps ``None`` to the same 404 a missing
    job gets)."""
    job = _get_job_row(db_path or default_db_path(), job_id)
    if job is None or job["owner"] != owner:
        return None
    return job


# ── magic-byte sniffing ──────────────────────────────────────────────────

def _riff_kind(head: bytes) -> Optional[Tuple[str, str]]:
    if len(head) < 12 or head[:4] != b"RIFF":
        return None
    subtype = head[8:12]
    if subtype == b"WAVE":
        return "audio", "audio/wav"
    if subtype == b"AVI ":
        return "video", "video/avi"
    if subtype == b"WEBP":
        return "image", "image/webp"
    return None


def _sniff_signature(head: bytes) -> Optional[Tuple[str, str]]:
    """This module's own magic-byte table — used when ``python-magic`` is
    not installed, exactly the fallback ``src/upload_handler.py`` already
    takes (CONTRATO.md rule 6: a missing optional dependency degrades to a
    documented, still-functional path, never a crash)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if head.startswith(b"BM"):
        return "image", "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image", "image/tiff"
    riff = _riff_kind(head)
    if riff is not None:
        return riff
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"  # EBML: webm or mkv, both video-kind here
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "video", "video/mp4"  # mp4/mov/m4v/m4a share this box; ffprobe
        # corrects an audio-only ftyp file to kind="audio" below.
    if head.startswith(b"fLaC"):
        return "audio", "audio/flac"
    if head.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if head.startswith(b"ID3"):
        return "audio", "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio", "audio/mpeg"  # frame-synced MP3 with no ID3 tag
    return None


def _sniff_kind(path: str, *, use_magic: bool = True) -> Tuple[str, str]:
    """The real kind/mime of the bytes at ``path``, verified from content —
    never from the caller's claimed filename or ``Content-Type``. Raises
    ``IngestValidationError`` for anything that is not recognisably an
    image/video/audio container, which is how a renamed non-media file
    ("MIME falsificado") is rejected."""
    with open(path, "rb") as fh:
        head = fh.read(64)
    if use_magic:
        try:
            import magic  # type: ignore
            mime = magic.Magic(mime=True).from_file(path)
        except Exception:  # noqa: BLE001 - not installed, or it could not read this file
            mime = None
        if mime:
            family = mime.split("/", 1)[0]
            if family in ("image", "video", "audio"):
                return family, mime
            # python-magic disagreed with our own header table too? Then
            # this really is not media; fall through to the signature table
            # only if magic could not decide (mime falsy), never override an
            # explicit non-media verdict from a real content sniffer.
            raise IngestValidationError(
                f"the file's real content type ({mime}) is not image/video/audio")
    sniffed = _sniff_signature(head)
    if sniffed is None:
        raise IngestValidationError(
            "could not verify this file's real type from its content "
            "(magic-byte sniff found no recognised image/video/audio header)")
    return sniffed


# ── ffprobe container validation ────────────────────────────────────────

def _probe_container(path: str, *, ffprobe: str,
                     run: Callable[..., Any]) -> Optional[Dict[str, Any]]:
    """Validate the container and read its duration/streams. Returns
    ``None`` when ffprobe itself could not make sense of the file (a
    malformed/truncated container) — the caller turns that into a rejection,
    never a fabricated "duration unknown, proceed anyway"."""
    try:
        proc = run([ffprobe, "-v", "quiet", "-print_format", "json",
                   "-show_format", "-show_streams", path],
                  capture_output=True, timeout=_FFPROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:  # docs/spec/creator/00_WP00_INVENTARIO.md §4 gap: check it
        return None
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    streams = data.get("streams") or []
    if not isinstance(streams, list) or not streams:
        return None
    has_video = any(s.get("codec_type") == "video" for s in streams if isinstance(s, dict))
    has_audio = any(s.get("codec_type") == "audio" for s in streams if isinstance(s, dict))
    fmt = data.get("format") or {}
    duration = None
    try:
        duration = float(fmt["duration"]) if fmt.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    return {"duration": duration, "has_video": has_video, "has_audio": has_audio}


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── copying validated bytes into the durable artifact store ────────────

def _copy_into_store(owner: str, project_id: str, source_path: str, filename: str,
                     kind: str, *, store_dir: Optional[str] = None) -> Dict[str, Any]:
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence

    store = store_dir or ARTIFACT_STORE_DIR
    digest, size, stored_name, _created = artifact_store.publish_copy(
        source_path, store, filename)
    media_type = mimetypes.guess_type(filename)[0] or f"{kind}/octet-stream"
    identity.ensure_blob(sha256=digest, byte_size=size, filename=stored_name,
                         media_type=media_type)

    candidate_id = "occ_" + hashlib.sha1(  # noqa: S324 - id, not a security digest
        f"{owner}:{project_id}:creator.ingest:{digest}".encode("utf-8")).hexdigest()[:24]
    occurrence = ArtifactOccurrence.parse({
        "id": candidate_id, "kind": kind, "blob_sha256": digest,
        "label": filename, "owner": owner, "project_id": project_id,
        "skill_id": "creator.ingest", "skill_version": "1.0.0",
        "provenance": {"backend": "creator.ingest", "recipe": "creator.ingest.upload",
                      "recipe_version": "1"},
        "retention": {"policy": "keep"},
    })
    recorded, _made = identity.ensure_occurrence(occurrence)
    return {"id": recorded.id, "kind": recorded.kind, "sha256": recorded.blob_sha256,
            "byte_size": size, "label": recorded.label, "project_id": recorded.project_id}


def _publish_derived(owner: str, source_occurrence_id: str, project_id: str,
                     out_path: str, *, out_kind: str, derived_label: str, recipe: str,
                     backend: str, store_dir: Optional[str] = None) -> Dict[str, Any]:
    """One owner-scoped derived occurrence, same shape as
    ``library.generate_proxy`` builds — a fresh identity every time
    (never shared across owners of identical bytes), carrying
    ``relations.derived_from=(source_occurrence_id,)`` per
    ``docs/03_ARQUITECTURA_Y_CONTRATOS.md`` line 33. Kept in this module
    (not `library.py`, which WP03 owns) because these two recipes
    (480p video preview, audio peaks JSON) are WP04-specific additions
    beyond WP03's thumbnail/frame/waveform trio."""
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR

    store = store_dir or ARTIFACT_STORE_DIR
    digest, size, stored_name, _created = artifact_store.publish_copy(
        out_path, store, os.path.basename(out_path))
    media_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
    identity.ensure_blob(sha256=digest, byte_size=size, filename=stored_name,
                         media_type=media_type)

    candidate_id = "occ_" + hashlib.sha1(  # noqa: S324 - id, not a security digest
        f"{owner}:{source_occurrence_id}:{recipe}".encode("utf-8")).hexdigest()[:24]
    from src.contracts.blob import ArtifactOccurrence
    occurrence = ArtifactOccurrence.parse({
        "id": candidate_id, "kind": out_kind, "blob_sha256": digest,
        "label": derived_label, "owner": owner, "project_id": project_id,
        "skill_id": "creator.ingest", "skill_version": "1.0.0",
        "provenance": {"backend": backend, "recipe": recipe, "recipe_version": "1",
                      "source_artifact_ids": [source_occurrence_id]},
        "retention": {"policy": "keep"},
        "relations": {"derived_from": [source_occurrence_id]},
    })
    recorded, made = identity.ensure_occurrence(occurrence)
    return {"occurrence_id": recorded.id, "kind": recorded.kind, "recipe": recipe,
            "created": made}


# ── proxies ──────────────────────────────────────────────────────────────

def _video_proxy_480p(source_path: str, tmp_dir: str, *, ffmpeg: str,
                      run: Callable[..., Any], duration: Optional[float]) -> str:
    out_path = os.path.join(tmp_dir, "proxy_480p.mp4")
    timeout = _FFMPEG_PROXY_TIMEOUT_S
    if duration:
        timeout = min(_FFMPEG_PROXY_TIMEOUT_S, max(30, int(duration) + 20))
    try:
        proc = run([ffmpeg, "-y", "-i", source_path,
                   "-vf", "scale=-2:480", "-c:v", "libx264",
                   "-b:v", "800k", "-maxrate", "800k", "-bufsize", "1600k",
                   "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                   out_path], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestValidationError(f"ffmpeg could not build the 480p preview: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise IngestValidationError(
            f"ffmpeg failed to build the 480p preview (exit {proc.returncode})")
    return out_path


def _audio_peaks(source_path: str, tmp_dir: str, *, ffmpeg: str,
                 run: Callable[..., Any], duration: Optional[float],
                 buckets: int = 800) -> Optional[str]:
    """Decode to mono 8kHz PCM (bounded), then min/max peaks per bucket via
    numpy when it is installed; ``None`` (no exception) when numpy is not
    available — a documented, silent skip, not a failed ingest."""
    try:
        import numpy as np  # noqa: F401 - availability probe only here
    except ImportError:
        logger.info("creator.ingest: numpy not installed; skipping audio peaks JSON")
        return None
    pcm_path = os.path.join(tmp_dir, "peaks.pcm")
    timeout = _FFMPEG_PEAKS_TIMEOUT_S
    if duration:
        timeout = min(_FFMPEG_PEAKS_TIMEOUT_S, max(20, int(duration) + 10))
    try:
        proc = run([ffmpeg, "-y", "-i", source_path, "-f", "s16le",
                   "-acodec", "pcm_s16le", "-ac", "1", "-ar", "8000",
                   pcm_path], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestValidationError(f"ffmpeg could not decode audio for peaks: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(pcm_path):
        raise IngestValidationError(
            f"ffmpeg failed to decode audio for peaks (exit {proc.returncode})")

    import numpy as np
    samples = np.fromfile(pcm_path, dtype="<i2").astype("float32") / 32768.0
    if samples.size == 0:
        peaks: List[List[float]] = []
    else:
        bucket_count = max(1, min(buckets, samples.size))
        chunks = np.array_split(samples, bucket_count)
        peaks = [[round(float(c.min()), 4), round(float(c.max()), 4)] for c in chunks]

    out_path = os.path.join(tmp_dir, "peaks.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"sample_rate": 8000, "channels": 1, "bucket_count": len(peaks),
                  "peaks": peaks}, fh)
    return out_path


def _build_proxies(owner: str, occurrence: Dict[str, Any], kind: str, *,
                   which: Callable[[str], Optional[str]], run: Callable[..., Any],
                   store_dir: Optional[str], duration: Optional[float] = None
                   ) -> List[Dict[str, Any]]:
    """Every proxy this ingest builds for one source occurrence. Image/video
    frame/audio waveform delegate to WP03's `library.generate_proxy` (never
    reimplemented here); the 480p video preview and audio peaks JSON are
    WP04's own. `ffmpeg` missing is a documented degrade (CONTRATO.md rule
    6) — logged and skipped, never a failed job."""
    from . import library

    proxies: List[Dict[str, Any]] = []
    if kind not in FILE_KINDS:
        return proxies

    try:
        base = library.generate_proxy(owner, occurrence["id"], store_dir=store_dir,
                                      which=which, run=run)
        proxies.append({"occurrence_id": base["occurrence"]["id"],
                        "kind": base["occurrence"]["kind"],
                        "recipe": base["occurrence"]["recipe"]})
    except library.ProxyUnavailable as exc:
        logger.info("creator.ingest: base proxy unavailable for %s: %s",
                   occurrence["id"], exc)

    if kind == "video":
        ffmpeg = which("ffmpeg")
        if not ffmpeg:
            logger.info("creator.ingest: ffmpeg not installed; skipping 480p preview")
        else:
            from src import artifact_identity as identity
            source_path = identity.path_for(occurrence["id"], owner=owner, store_dir=store_dir)
            tmp_dir = tempfile.mkdtemp(prefix="creator-ingest-")
            try:
                out_path = _video_proxy_480p(source_path, tmp_dir, ffmpeg=ffmpeg,
                                             run=run, duration=duration)
                derived = _publish_derived(
                    owner, occurrence["id"], occurrence.get("project_id", ""), out_path,
                    out_kind="video", derived_label=f"480p preview of {occurrence.get('label', occurrence['id'])}",
                    recipe="creator.ingest.video_proxy_480p", backend="ffmpeg", store_dir=store_dir)
                proxies.append({"occurrence_id": derived["occurrence_id"], "kind": "video",
                                "recipe": derived["recipe"]})
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    if kind == "audio":
        ffmpeg = which("ffmpeg")
        if not ffmpeg:
            logger.info("creator.ingest: ffmpeg not installed; skipping audio peaks JSON")
        else:
            from src import artifact_identity as identity
            source_path = identity.path_for(occurrence["id"], owner=owner, store_dir=store_dir)
            tmp_dir = tempfile.mkdtemp(prefix="creator-ingest-")
            try:
                out_path = _audio_peaks(source_path, tmp_dir, ffmpeg=ffmpeg,
                                        run=run, duration=duration)
                if out_path:
                    derived = _publish_derived(
                        owner, occurrence["id"], occurrence.get("project_id", ""), out_path,
                        out_kind="json", derived_label=f"peaks of {occurrence.get('label', occurrence['id'])}",
                        recipe="creator.ingest.audio_peaks", backend="ffmpeg+numpy", store_dir=store_dir)
                    proxies.append({"occurrence_id": derived["occurrence_id"], "kind": "json",
                                    "recipe": derived["recipe"]})
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return proxies


# ── file ingestion ───────────────────────────────────────────────────────

def ingest_file(owner: str, project_id: str, source_path: str, filename: str, *,
                max_bytes: Optional[int] = None, max_duration_s: Optional[float] = None,
                db_path: Optional[str] = None, store_dir: Optional[str] = None,
                which: Callable[[str], Optional[str]] = shutil.which,
                run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Validate, then durably ingest, one local file already on disk
    (an upload's own temp file, or any caller-resolved local path).

    Every check in the module docstring runs before ``source_path`` is
    copied anywhere or before any job row is created — a rejection here
    leaves nothing behind, ever. Idempotent by content: two calls with
    identical bytes for the same owner/project return the SAME job
    (the second one immediately, without re-copying or re-proxying, once
    the first reaches ``done``)."""
    if not project_id:
        raise ValueError("project_id is required")
    db_path = db_path or default_db_path()
    max_bytes = max_bytes if max_bytes is not None else max_ingest_bytes()
    max_duration = max_duration_s if max_duration_s is not None else max_ingest_duration_s()

    try:
        size = os.path.getsize(source_path)
    except OSError as exc:
        raise IngestValidationError(f"source file is not readable: {exc}") from exc
    if size <= 0:
        raise IngestValidationError("source file is empty")
    if size > max_bytes:
        raise IngestValidationError(
            f"file is {size} bytes, over the {max_bytes} byte ingest limit")

    kind, _mime = _sniff_kind(source_path)

    duration: Optional[float] = None
    if kind in ("video", "audio"):
        ffprobe = which("ffprobe")
        if ffprobe:
            probed = _probe_container(source_path, ffprobe=ffprobe, run=run)
            if probed is None:
                raise IngestValidationError(
                    "ffprobe could not validate this container; it may be malformed")
            duration = probed["duration"]
            if kind == "video" and probed["has_audio"] and not probed["has_video"]:
                kind = "audio"  # an ftyp box with only an audio stream (m4a)
            if duration is not None and duration > max_duration:
                raise IngestValidationError(
                    f"media duration {duration:.1f}s exceeds the {max_duration:.0f}s "
                    "ingest limit")
        else:
            logger.info("creator.ingest: ffprobe not installed; duration was not verified")

    sha256 = _sha256_of(source_path)
    job_id = _job_id(owner, project_id, sha256)
    job = _get_job_row(db_path, job_id)
    if job is None:
        job = _create_job(db_path, job_id, owner=owner, project_id=project_id,
                          source_kind="file", source_label=filename, content_key=sha256)
    if job["owner"] != owner:
        raise IngestJobNotFound(job_id)  # a sha collision across owners: never leak
    if job["state"] == "done":
        return job

    _update_job(db_path, job_id, state="validating")

    try:
        occurrence = _copy_into_store(owner, project_id, source_path, filename, kind,
                                      store_dir=store_dir)
    except Exception as exc:  # noqa: BLE001 - persisted for the /ingest/{job_id} caller
        _update_job(db_path, job_id, state="failed", error=str(exc))
        raise

    _update_job(db_path, job_id, state="proxying", occurrence_id=occurrence["id"])

    proxies = _build_proxies(owner, occurrence, kind, which=which, run=run,
                             store_dir=store_dir, duration=duration)

    _update_job(db_path, job_id, state="done", occurrence_id=occurrence["id"], proxies=proxies)
    return _get_job_row(db_path, job_id)  # type: ignore[return-value]


# ── URL ingestion ────────────────────────────────────────────────────────

async def ingest_url(owner: str, project_id: str, url: str, *,
                     db_path: Optional[str] = None,
                     store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Bounded ingestion of a URL: YouTube goes through the existing
    ``services.youtube.youtube_handler`` (transcript, not the video bytes —
    RES06/QA07: this never downloads to "the cloud" or grants folder
    access, it makes one outbound read of a page Faustus already reads for
    chat); any other URL goes through ``src.reach.router`` (the channel it
    already resolves to — web, github, reddit, ...), bounded to
    ``MAX_URL_TEXT_CHARS``. Either way the result is stored as a `text`
    occurrence, never executed, never treated as a script (docs/09:
    "tratar SVG/HTML como contenido activo hasta sanitizar" — this module
    only ever extracts plain text, never renders or runs the page)."""
    if not project_id:
        raise ValueError("project_id is required")
    if not isinstance(url, str) or not url.strip():
        raise IngestValidationError("a URL is required")
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise IngestValidationError("only http(s) URLs are supported")

    db_path = db_path or default_db_path()
    content_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    job_id = _job_id(owner, project_id, content_key)
    job = _get_job_row(db_path, job_id)
    if job is None:
        job = _create_job(db_path, job_id, owner=owner, project_id=project_id,
                          source_kind="url", source_label=url, content_key=content_key)
    if job["owner"] != owner:
        raise IngestJobNotFound(job_id)
    if job["state"] == "done":
        return job

    _update_job(db_path, job_id, state="validating")

    try:
        from services.youtube import youtube_handler as yt

        if yt.is_youtube_url(url):
            video_id = yt.extract_youtube_id(url)
            if not video_id:
                raise IngestValidationError("could not extract a YouTube video id from this URL")
            if not yt.YOUTUBE_AVAILABLE:
                yt.init_youtube()
            result = await yt.extract_transcript_async(url, video_id)
            if not result.get("success"):
                raise IngestValidationError(
                    f"could not fetch a transcript for this video: {result.get('error') or 'unknown error'}")
            text = (result.get("transcript") or "")[:MAX_URL_TEXT_CHARS]
            title = f"YouTube transcript: {video_id}"
            source_trust = "public_api"
        else:
            from src.reach import router as reach_router
            reach_result = await reach_router.read(url)
            if reach_result.error or not (reach_result.text or "").strip():
                raise IngestValidationError(
                    f"could not fetch readable text from this URL: {reach_result.error or 'empty result'}")
            text = reach_result.text[:MAX_URL_TEXT_CHARS]
            title = reach_result.title or url
            source_trust = reach_result.source_trust
    except IngestValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - a fetch failure fails the job, not the caller's process
        _update_job(db_path, job_id, state="failed", error=str(exc))
        raise IngestValidationError(f"could not fetch this URL: {exc}") from exc

    occurrence = await _store_url_text(owner, project_id, url, title, text,
                                       source_trust=source_trust, store_dir=store_dir)
    _update_job(db_path, job_id, state="done", occurrence_id=occurrence["id"])
    return _get_job_row(db_path, job_id)  # type: ignore[return-value]


async def _store_url_text(owner: str, project_id: str, url: str, title: str, text: str, *,
                          source_trust: str, store_dir: Optional[str]) -> Dict[str, Any]:
    import asyncio

    def _write_and_publish() -> Dict[str, Any]:
        from src import artifact_identity as identity
        from src import artifact_store
        from src.constants import ARTIFACT_STORE_DIR
        from src.contracts.blob import ArtifactOccurrence

        store = store_dir or ARTIFACT_STORE_DIR
        os.makedirs(store, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".ingest-url-", suffix=".txt", dir=store)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            digest, size, stored_name, _created = artifact_store.publish_copy(
                tmp_path, store, "ingested_url.txt")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        identity.ensure_blob(sha256=digest, byte_size=size, filename=stored_name,
                             media_type="text/plain")
        candidate_id = "occ_" + hashlib.sha1(  # noqa: S324
            f"{owner}:{project_id}:creator.ingest.url:{digest}".encode("utf-8")).hexdigest()[:24]
        occurrence = ArtifactOccurrence.parse({
            "id": candidate_id, "kind": "text", "blob_sha256": digest,
            "label": title, "owner": owner, "project_id": project_id,
            "skill_id": "creator.ingest", "skill_version": "1.0.0",
            "provenance": {"backend": "creator.ingest", "recipe": "creator.ingest.url",
                          "recipe_version": "1", "note": f"source_url={url} trust={source_trust}"},
            "retention": {"policy": "keep"},
        })
        recorded, _made = identity.ensure_occurrence(occurrence)
        return {"id": recorded.id, "kind": recorded.kind, "sha256": recorded.blob_sha256,
                "byte_size": size, "label": recorded.label, "project_id": recorded.project_id}

    return await asyncio.to_thread(_write_and_publish)


# ── resumability ─────────────────────────────────────────────────────────

def resume_job(owner: str, job_id: str, *, db_path: Optional[str] = None,
               store_dir: Optional[str] = None,
               which: Callable[[str], Optional[str]] = shutil.which,
               run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Finish a job left at ``proxying`` after a crash/restart. The source
    occurrence is already durable (bytes on disk, owned) by the time a job
    reaches this state, so this never re-reads the original upload — only
    ``src.artifact_identity``, exactly like a fresh ``GET`` would.

    A job at any other state is returned unchanged: ``done``/``failed`` have
    nothing left to do, and ``pending``/``validating`` have no durable
    source yet to resume from — the caller re-submits the same bytes/URL
    (idempotent, see the module docstring) instead."""
    db_path = db_path or default_db_path()
    job = _get_job_row(db_path, job_id)
    if job is None or job["owner"] != owner:
        raise IngestJobNotFound(job_id)
    if job["state"] != "proxying" or not job["occurrence_id"]:
        return job

    from src import artifact_identity as identity
    try:
        occ = identity.for_owner(job["occurrence_id"], owner=owner)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        _update_job(db_path, job_id, state="failed", error=str(exc))
        raise IngestJobNotFound(job_id) from exc

    occurrence = {"id": occ.id, "kind": occ.kind, "label": occ.label,
                 "project_id": occ.project_id or ""}
    proxies = _build_proxies(owner, occurrence, occ.kind, which=which, run=run,
                             store_dir=store_dir)
    _update_job(db_path, job_id, state="done", occurrence_id=occ.id, proxies=proxies)
    return _get_job_row(db_path, job_id)  # type: ignore[return-value]


def resume_pending(*, owner: Optional[str] = None, db_path: Optional[str] = None,
                   store_dir: Optional[str] = None,
                   which: Callable[[str], Optional[str]] = shutil.which,
                   run: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
    """Sweep every job left at ``proxying`` (optionally scoped to one
    owner) and resume each. Meant to run once at process startup so a crash
    mid-proxy is repaired without a human re-triggering every job by hand;
    a job whose own resume raises is logged and skipped, never allowed to
    stop the sweep for every other job."""
    db_path = db_path or default_db_path()
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        if owner:
            rows = conn.execute(
                "SELECT id, owner FROM ingest_jobs WHERE state='proxying' AND owner=?",
                (owner,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, owner FROM ingest_jobs WHERE state='proxying'").fetchall()
    resumed: List[Dict[str, Any]] = []
    for row in rows:
        try:
            resumed.append(resume_job(row["owner"], row["id"], db_path=db_path,
                                      store_dir=store_dir, which=which, run=run))
        except Exception:  # noqa: BLE001 - one bad job must not stop the sweep
            logger.exception("creator.ingest: resume_pending failed for job %s", row["id"])
    return resumed


__all__ = [
    "IngestValidationError", "IngestJobNotFound", "FILE_KINDS", "MAX_URL_TEXT_CHARS",
    "default_db_path", "max_ingest_bytes", "max_ingest_duration_s",
    "ingest_file", "ingest_url", "get_job", "resume_job", "resume_pending",
]
