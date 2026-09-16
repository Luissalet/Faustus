"""library.py — Creator's per-project media library (WP03).

CONTRATO.md rule 1 and ``docs/spec/creator/plan/docs/02_CREATOR_PRODUCTO.md``
line 15 are explicit: "La biblioteca debe ser la misma colección de
occurrences que usan Activity y context links." This module is therefore NOT
a new store of media rows — it is a project-scoped read over
``src.artifact_catalog`` (itself a projection over ``src.artifact_identity``'s
blob/occurrence tables), plus exactly one small addition: a cache of media
metadata (duration, dimensions, codec, sample_rate) keyed by blob sha256,
because that is the one fact the identity/catalog layer genuinely does not
have and genuinely should not recompute on every list call.

Proxies (thumbnails, video frames, waveforms) are the other new behaviour
here. ``docs/03_ARQUITECTURA_Y_CONTRATOS.md`` line 33 says a proxy carries a
``derived_from`` relation and a recipe, and is never "una nueva identidad
compartida entre propietarios" — so a proxy this module builds is always a
brand-new ``ArtifactOccurrence`` owned by the SAME owner as its source, with
``relations.derived_from=(source_id,)``, recorded through
``src.artifact_identity`` exactly like any other occurrence. It deliberately
does NOT use ``src.artifact_identity.record_derivative``/``DerivedArtifactRow``
— that table hangs a derivative off the BLOB by design (one thumbnail shared
by every owner of identical bytes), which is precisely the sharing AST02's
acceptance criterion ("Bytes idénticos no comparten permisos") forbids for a
per-owner Library proxy.
"""
from __future__ import annotations

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
from typing import Any, Callable, Dict, Iterator, List, Optional

from src.contracts.base import now_iso

from .errors import CreatorError

logger = logging.getLogger(__name__)

#: The kinds this library extracts media metadata for and builds proxies of.
#: Every other kind (document, code, dataset, ...) still lists fine; it just
#: never gets a `media` block or a `/proxy` route.
MEDIA_KINDS = ("image", "video", "audio")

_BUSY_TIMEOUT_S = 30


class LibraryItemNotFound(CreatorError):
    """No occurrence with that id, or it belongs to someone else.

    Deliberately the same exception (and the same 404) for both, per
    CONTRATO.md rule 3: "no es tuyo" y "no existe" responden igual.
    """


class ProxyUnavailable(CreatorError):
    """The engine a proxy kind needs (today: ffmpeg for video/audio) is not
    installed. Mapped to 409 by the route, documented rather than silently
    degraded (CONTRATO.md rule 6)."""


# ── metadata cache: DATA_DIR/creator/library.db ─────────────────────────────

def default_db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "library.db")


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
            "CREATE TABLE IF NOT EXISTS media_metadata ("
            "sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "width INTEGER, height INTEGER, duration REAL, "
            "codec TEXT, sample_rate INTEGER, channels INTEGER, "
            "probe_tool TEXT NOT NULL, raw_json TEXT, extracted_at REAL NOT NULL)"
        )


def _cached_metadata(db_path: str, sha256: str) -> Optional[Dict[str, Any]]:
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM media_metadata WHERE sha256=?", (sha256,)).fetchone()
    if row is None:
        return None
    return {"kind": row["kind"], "width": row["width"], "height": row["height"],
            "duration": row["duration"], "codec": row["codec"],
            "sample_rate": row["sample_rate"], "channels": row["channels"],
            "probe_tool": row["probe_tool"], "cached": True}


def _store_metadata(db_path: str, sha256: str, kind: str, meta: Dict[str, Any],
                    probe_tool: str) -> None:
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO media_metadata (sha256, kind, width, height, duration, "
            "codec, sample_rate, channels, probe_tool, raw_json, extracted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sha256) DO UPDATE SET kind=excluded.kind, "
            "width=excluded.width, height=excluded.height, duration=excluded.duration, "
            "codec=excluded.codec, sample_rate=excluded.sample_rate, "
            "channels=excluded.channels, probe_tool=excluded.probe_tool, "
            "raw_json=excluded.raw_json, extracted_at=excluded.extracted_at",
            (sha256, kind, meta.get("width"), meta.get("height"), meta.get("duration"),
             meta.get("codec"), meta.get("sample_rate"), meta.get("channels"),
             probe_tool,
             json.dumps(meta.get("raw")) if meta.get("raw") is not None else None,
             time.time()))


def _probe_image(path: str) -> Optional[Dict[str, Any]]:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        logger.debug("could not probe image dimensions for %s", path, exc_info=True)
        return None
    return {"width": width, "height": height}


def _probe_ffprobe(path: str, *, ffprobe: str,
                   run: Callable[..., Any]) -> Optional[Dict[str, Any]]:
    try:
        proc = run([ffprobe, "-v", "quiet", "-print_format", "json",
                   "-show_format", "-show_streams", path],
                  capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # CONTRATO.md rule 6: check the returncode before trusting stdout, unlike
    # media_capabilities._run_version (docs/spec/creator/00_WP00_INVENTARIO.md
    # §4 names that gap; this module does not repeat it).
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    fmt = data.get("format") or {}
    duration = None
    try:
        duration = float(fmt["duration"]) if fmt.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out: Dict[str, Any] = {"duration": duration, "raw": data}
    if video:
        out["width"] = video.get("width")
        out["height"] = video.get("height")
        out["codec"] = video.get("codec_name")
    if audio:
        try:
            out["sample_rate"] = int(audio["sample_rate"]) if audio.get("sample_rate") else None
        except (TypeError, ValueError):
            out["sample_rate"] = None
        out["channels"] = audio.get("channels")
        out.setdefault("codec", audio.get("codec_name"))
    return out


def extract_metadata(*, sha256: str, path: str, kind: str,
                     db_path: Optional[str] = None,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Duration/dimensions/codec/sample_rate for one blob, cached by sha256 so
    two owners of identical bytes (or two occurrences of one re-used render)
    probe it once. Images use PIL; video/audio use ffprobe when
    ``shutil.which("ffprobe")`` finds it, honouring its returncode — no probe
    tool installed is a real, expected answer (an empty-ish record), never an
    exception."""
    db_path = db_path or default_db_path()
    cached = _cached_metadata(db_path, sha256)
    if cached is not None:
        return cached
    meta: Dict[str, Any] = {}
    probe_tool = "none"
    if kind == "image":
        probed = _probe_image(path)
        if probed:
            meta.update(probed)
            probe_tool = "pillow"
    elif kind in ("video", "audio"):
        ffprobe = which("ffprobe")
        if ffprobe:
            probed = _probe_ffprobe(path, ffprobe=ffprobe, run=run)
            if probed is not None:
                meta.update(probed)
                probe_tool = "ffprobe"
    _store_metadata(db_path, sha256, kind, meta, probe_tool)
    return {"kind": kind, "width": meta.get("width"), "height": meta.get("height"),
            "duration": meta.get("duration"), "codec": meta.get("codec"),
            "sample_rate": meta.get("sample_rate"), "channels": meta.get("channels"),
            "probe_tool": probe_tool, "cached": False}


# ── listing ──────────────────────────────────────────────────────────────

def _public_row(row) -> Dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "label": row.label or row.filename,
            "sha256": row.sha256, "byte_size": row.byte_size,
            "media_type": row.media_type, "project_id": row.project_id or "",
            "run_id": row.run_id or "",
            "recipe": getattr(row, "recipe", None) or "",
            "skill_id": getattr(row, "skill_id", None) or "",
            "partial": bool(row.partial), "created_at": str(row.created_at),
            "download_url": f"/api/artifacts/{row.id}/download"}


def list_items(owner: str, project_id: str, *, kind: str = "", tag: str = "",
               q: str = "", db_path: Optional[str] = None,
               which: Callable[[str], Optional[str]] = shutil.which,
               run: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
    """This owner's occurrences in one project, optionally narrowed by
    ``kind``, a ``tag`` (matched against ``recipe`` or ``skill_id`` — no
    separate tag column exists in the identity schema today, see the report)
    and a free-text ``q`` (substring on the label, delegated to
    ``artifact_catalog.recent``). Media kinds get a ``media`` block with
    duration/dimensions/codec/sample_rate; every other kind gets ``None``."""
    if not project_id:
        raise ValueError("project_id is required")
    from core.database import SessionLocal
    from src import artifact_catalog

    with SessionLocal() as db:
        rows = artifact_catalog.recent(db, owner=owner, project_id=project_id,
                                       kind=kind, q=q, limit=200)
    items: List[Dict[str, Any]] = []
    for row in rows:
        recipe = getattr(row, "recipe", None) or ""
        skill_id = getattr(row, "skill_id", None) or ""
        if tag and tag not in (recipe, skill_id):
            continue
        entry = _public_row(row)
        entry["media"] = None
        if row.kind in MEDIA_KINDS and row.sha256:
            try:
                path = artifact_catalog.path(row)
            except (ValueError, TypeError):
                path = None
            if path and os.path.isfile(path):
                entry["media"] = extract_metadata(
                    sha256=row.sha256, path=path, kind=row.kind,
                    db_path=db_path, which=which, run=run)
        items.append(entry)
    return items


# ── proxies ──────────────────────────────────────────────────────────────

def _image_thumbnail(source_path: str, tmp_dir: str, *, max_side: int = 512) -> str:
    from PIL import Image
    out_path = os.path.join(tmp_dir, "thumbnail.png")
    with Image.open(source_path) as img:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        img.save(out_path, format="PNG")
    return out_path


def _video_thumbnail(source_path: str, tmp_dir: str, *, ffmpeg: str,
                     run: Callable[..., Any]) -> str:
    out_path = os.path.join(tmp_dir, "thumbnail.png")
    try:
        proc = run([ffmpeg, "-y", "-ss", "0", "-i", source_path,
                   "-frames:v", "1", out_path], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxyUnavailable(f"ffmpeg could not run: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise ProxyUnavailable(
            f"ffmpeg failed to extract a video frame (exit {proc.returncode})")
    return out_path


def _audio_waveform(source_path: str, tmp_dir: str, *, ffmpeg: str,
                    run: Callable[..., Any]) -> str:
    out_path = os.path.join(tmp_dir, "waveform.png")
    try:
        proc = run([ffmpeg, "-y", "-i", source_path, "-filter_complex",
                   "showwavespic=s=640x120", "-frames:v", "1", out_path],
                  capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxyUnavailable(f"ffmpeg could not run: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise ProxyUnavailable(
            f"ffmpeg failed to render a waveform (exit {proc.returncode})")
    return out_path


def _public_occurrence(occ) -> Dict[str, Any]:
    return {"id": occ.id, "kind": occ.kind, "sha256": occ.blob_sha256,
            "label": occ.label, "project_id": occ.project_id or "",
            "recipe": occ.provenance.recipe or "",
            "derived_from": list(occ.relations.derived_from),
            "download_url": f"/api/artifacts/{occ.id}/download"}


def generate_proxy(owner: str, occurrence_id: str, *,
                   store_dir: Optional[str] = None,
                   which: Optional[Callable[[str], Optional[str]]] = None,
                   run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Build (or return, if already built) one owner-scoped proxy occurrence
    derived from ``occurrence_id``. Image proxies use PIL and always
    succeed for a readable image; video/audio proxies need ``ffmpeg`` and
    raise ``ProxyUnavailable`` (409) when it is not installed — documented
    degradation, never a silent no-op or a fabricated success."""
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence

    which = which or shutil.which  # resolved at call time, not at def time, so a
    # test (or a route) that monkeypatches `shutil.which` after import still
    # takes effect for a caller that did not pass its own `which`.
    try:
        source = identity.for_owner(occurrence_id, owner=owner)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        raise LibraryItemNotFound(str(exc)) from exc
    if source.kind not in MEDIA_KINDS:
        raise ValueError(f"cannot build a proxy for kind={source.kind!r}")

    source_path = identity.path_for(occurrence_id, owner=owner, store_dir=store_dir)

    tmp_dir = tempfile.mkdtemp(prefix="creator-proxy-")
    try:
        if source.kind == "image":
            proxy_path = _image_thumbnail(source_path, tmp_dir)
            derived_kind, recipe, out_kind, backend = (
                "thumbnail", "creator.proxy.image_thumbnail", "image", "pillow")
        else:
            ffmpeg = which("ffmpeg")
            if not ffmpeg:
                raise ProxyUnavailable(
                    "ffmpeg is not installed; video/audio proxies are unavailable "
                    "(install ffmpeg to enable this)")
            if source.kind == "video":
                proxy_path = _video_thumbnail(source_path, tmp_dir, ffmpeg=ffmpeg, run=run)
                derived_kind, recipe = "thumbnail", "creator.proxy.video_frame"
            else:
                proxy_path = _audio_waveform(source_path, tmp_dir, ffmpeg=ffmpeg, run=run)
                derived_kind, recipe = "waveform", "creator.proxy.audio_waveform"
            out_kind, backend = "image", "ffmpeg"

        store = store_dir or ARTIFACT_STORE_DIR
        digest, size, filename, _created = artifact_store.publish_copy(
            proxy_path, store, os.path.basename(proxy_path))
        identity.ensure_blob(sha256=digest, byte_size=size, filename=filename,
                             media_type=mimetypes.guess_type(filename)[0] or "image/png")

        import hashlib
        params_hash = hashlib.sha256(
            f"{source.id}:{recipe}".encode("utf-8")).hexdigest()
        candidate_id = "occ_" + hashlib.sha1(  # noqa: S324 - id, not a security digest
            f"{owner}:{source.id}:{recipe}".encode("utf-8")).hexdigest()[:24]
        occurrence = ArtifactOccurrence.parse({
            "id": candidate_id, "kind": out_kind, "blob_sha256": digest,
            "label": f"{derived_kind} of {source.label or source.id}",
            "owner": owner, "project_id": source.project_id,
            "run_id": source.run_id,
            "skill_id": "creator.library", "skill_version": "1.0.0",
            "provenance": {"backend": backend, "recipe": recipe,
                          "recipe_version": "1", "recipe_fingerprint": params_hash,
                          "source_artifact_ids": [source.id]},
            "retention": {"policy": "keep"},
            "relations": {"derived_from": [source.id]},
        })
        recorded, made = identity.ensure_occurrence(occurrence)
        return {"occurrence": _public_occurrence(recorded), "created": made}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── export manifest ────────────────────────────────────────────────────────

def export_manifest(owner: str, project_id: str) -> Dict[str, Any]:
    """AST01 / ``docs/03_ARQUITECTURA_Y_CONTRATOS.md`` line 33: "Un export de
    proyecto enumera occurrences concretas, no sólo hashes globales." Every
    entry names its own occurrence id and exact bytes (sha256 + byte_size),
    never just a hash two owners could share, plus its recipe and its direct
    ``derived_from`` parents."""
    if not project_id:
        raise ValueError("project_id is required")
    from . import lineage as lineage_mod

    items = list_items(owner, project_id)
    entries = []
    for item in items:
        try:
            parents = lineage_mod.direct_parents(owner, item["id"])
        except lineage_mod.LineageItemNotFound:
            parents = []
        entries.append({
            "occurrence_id": item["id"], "kind": item["kind"],
            "sha256": item["sha256"], "byte_size": item["byte_size"],
            "label": item["label"], "recipe": item["recipe"],
            "derived_from": parents,
        })
    return {"project_id": project_id, "generated_at": now_iso(), "occurrences": entries}


__all__ = [
    "LibraryItemNotFound", "ProxyUnavailable", "MEDIA_KINDS",
    "default_db_path", "extract_metadata", "list_items",
    "generate_proxy", "export_manifest",
]
