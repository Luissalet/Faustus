# src/meetings.py
"""Meeting notes (FEATURE B): audio in, Markdown notes out.

Pipeline, run as a background job so an upload never holds an HTTP request
open for the length of the recording:

  1. The uploaded audio is split into fixed-length chunks with ``ffmpeg``
     (falls back to treating the whole file as one chunk when ffmpeg is not
     on PATH — a single-chunk transcription is still correct, just not
     memory-friendly for a very long recording).
  2. Each chunk is transcribed with the STT engine already configured for
     the instance (``services.stt.stt_service`` — faster-whisper when the
     "local" provider is selected, same as chat dictation), through
     ``STTService.transcribe_segments`` — which already runs every segment
     through ``src/stt_cleanup.py`` before it comes back here, so this
     module never sees raw Whisper hallucinations.
  3. Chunk segments are concatenated with a running time offset so the
     final transcript's timestamps read as one continuous recording.
  4. One local-model pass (``src/task_endpoint.py`` — the same
     Background-Tasks/Utility endpoint resolution chain other internal
     passes such as e-mail translation and the news brief already use)
     turns the cleaned transcript into Markdown notes: summary, decisions,
     action items, open questions. If that pass fails or is unavailable,
     the note is still saved — transcript-only, with a warning recorded in
     the sidecar and shown at the top of the Markdown — a broken model
     endpoint must never lose the transcript.
  5. The Markdown goes to ``DATA_DIR/meetings/<date>-<slug>.md`` and a JSON
     sidecar of the same name carries the structured metadata (owner,
     duration, cleanup stats, warnings, optional project id) that the list/
     detail routes read without re-parsing Markdown.

Jobs are tracked in ``DATA_DIR/meetings/jobs.json`` (one dict, rewritten
atomically) so a restart mid-transcription leaves an honest "failed" status
rather than a job that silently vanishes — the same durability shape
``src/bg_jobs.py`` uses for shell jobs, kept separate here because a meeting
job runs Python (the STT + notes pipeline), not a subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR
from src import stt_cleanup

logger = logging.getLogger(__name__)

MEETINGS_DIR = os.path.join(DATA_DIR, "meetings")
JOBS_FILE = os.path.join(MEETINGS_DIR, "jobs.json")
_TMP_DIR = os.path.join(MEETINGS_DIR, "_jobs")

_LOCK = threading.RLock()

#: How long one chunk is, in seconds, when ffmpeg is available to split the
#: recording. Configurable via the ``meetings_chunk_seconds`` setting.
DEFAULT_CHUNK_SECONDS = 600

#: Transcript characters handed to the notes model. Long enough for a real
#: meeting, short enough to stay inside a small local model's context.
NOTES_TRANSCRIPT_CHAR_BUDGET = 32000

JOB_STATUSES = ("queued", "running", "done", "failed")


def _ensure_dirs() -> None:
    os.makedirs(MEETINGS_DIR, exist_ok=True)
    os.makedirs(_TMP_DIR, exist_ok=True)


def slugify(text: str, limit: int = 60) -> str:
    """ASCII-ish slug for a filename: accents folded, everything else
    collapsed to ``-``."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-").lower()
    return (folded or "meeting")[:limit]


# ── Job store ────────────────────────────────────────────────────────────

def _load_jobs() -> Dict[str, Dict[str, Any]]:
    try:
        if os.path.exists(JOBS_FILE):
            data = json.loads(Path(JOBS_FILE).read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_jobs(jobs: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dirs()
    atomic_write_json(JOBS_FILE, jobs, indent=2)


def _update_job(job_id: str, **fields: Any) -> None:
    with _LOCK:
        jobs = _load_jobs()
        job = jobs.get(job_id, {})
        job.update(fields)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        jobs[job_id] = job
        _save_jobs(jobs)


def get_job(job_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    jobs = _load_jobs()
    job = jobs.get(job_id)
    if not job:
        return None
    if owner is not None and job.get("owner", "") != (owner or ""):
        return None
    return job


# ── Audio chunking ───────────────────────────────────────────────────────

def _probe_duration(path: str) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _split_chunks(src_path: str, work_dir: str, chunk_seconds: int) -> List[Dict[str, Any]]:
    """Return ``[{"path": ..., "offset": seconds}, ...]``. Each chunk is a
    16kHz mono WAV re-encoded from ``[offset, offset+chunk_seconds)`` of the
    source — accurate seeking (unlike ``-c copy`` segmenting) at the cost of
    a decode pass ffmpeg was going to do anyway for a non-WAV upload.

    Falls back to a single chunk (the whole file, offset 0) when ffmpeg is
    missing or duration probing fails — still correct, just not chunked.
    """
    ffmpeg = shutil.which("ffmpeg")
    duration = _probe_duration(src_path) if ffmpeg else None
    if not ffmpeg or not duration:
        return [{"path": src_path, "offset": 0.0}]

    chunks: List[Dict[str, Any]] = []
    start = 0.0
    index = 0
    while start < duration:
        out_path = os.path.join(work_dir, f"chunk_{index:03d}.wav")
        cmd = [ffmpeg, "-y", "-ss", str(start), "-t", str(chunk_seconds),
               "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 44:  # header-only WAV = empty
                chunks.append({"path": out_path, "offset": start})
        except Exception as e:  # noqa: BLE001
            logger.warning("meetings: chunk %d split failed (%s); stopping split at %.1fs", index, e, start)
            break
        start += chunk_seconds
        index += 1

    return chunks or [{"path": src_path, "offset": 0.0}]


# ── Transcription ────────────────────────────────────────────────────────

def _transcribe_chunks(chunks: List[Dict[str, Any]], *, language: str, stt_service) -> Dict[str, Any]:
    all_segments: List[Dict[str, Any]] = []
    warnings: List[str] = []
    agg_stats: Dict[str, int] = {}

    for chunk in chunks:
        try:
            audio_bytes = Path(chunk["path"]).read_bytes()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Could not read audio chunk at {chunk.get('offset', 0):.0f}s: {e}")
            continue

        metadata: Dict[str, Any] = {}
        try:
            segments = stt_service.transcribe_segments(audio_bytes, language=language, metadata=metadata)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Transcription failed for the chunk at {chunk.get('offset', 0):.0f}s: {e}")
            continue

        if segments is None:
            warnings.append(f"STT unavailable for the chunk at {chunk.get('offset', 0):.0f}s.")
            continue

        offset = float(chunk.get("offset") or 0.0)
        for seg in segments:
            start = seg.get("start")
            end = seg.get("end")
            all_segments.append({
                "start": (start + offset) if start is not None else None,
                "end": (end + offset) if end is not None else None,
                "text": seg.get("text", ""),
            })

        for k, v in (metadata.get("cleanup_stats") or {}).items():
            agg_stats[k] = agg_stats.get(k, 0) + (v or 0)

    return {"segments": all_segments, "warnings": warnings, "cleanup_stats": agg_stats}


def _transcript_lines(segments: List[Dict[str, Any]]) -> List[str]:
    return [f"[{stt_cleanup.format_timestamp(s.get('start'))}] {s.get('text', '').strip()}"
            for s in segments if s.get("text", "").strip()]


# ── Notes generation ─────────────────────────────────────────────────────

_NOTES_SYSTEM_PROMPT = (
    "You are taking notes for a meeting from its transcript. Read the "
    "transcript and write ONLY the following Markdown sections, in this "
    "order, with these exact headings:\n\n"
    "## Summary\nTwo to five sentences of what the meeting was about and "
    "what was covered.\n\n"
    "## Decisions\nA bullet list of decisions that were made. If none were "
    "made, write a single bullet: \"None recorded.\"\n\n"
    "## Action items\nA bullet list, one per action. Include the owner in "
    "parentheses and the due date if either was stated in the transcript; "
    "omit what was not stated. If there are none, write a single bullet: "
    "\"None recorded.\"\n\n"
    "## Open questions\nA bullet list of unresolved questions raised in the "
    "meeting. If there are none, write a single bullet: \"None recorded.\"\n\n"
    "Do not include the transcript itself, do not add any other section, "
    "and do not invent anything not stated in the transcript. Respond in "
    "the same language as the transcript."
)


_LANGUAGE_NAMES = {"es": "Spanish", "en": "English", "fr": "French", "de": "German",
                   "it": "Italian", "pt": "Portuguese", "ca": "Catalan"}


_STOPWORDS = {
    "es": {"de", "que", "la", "el", "los", "las", "en", "y", "por", "para", "con", "una"},
    "en": {"the", "and", "of", "to", "is", "that", "for", "with", "we", "you", "this"},
}


def _guess_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúñü]+", (text or "").lower())[:2000]
    if len(words) < 20:
        return ""
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else ""


def _notes_prompt(language: str = "") -> str:
    name = _LANGUAGE_NAMES.get((language or "").lower()[:2])
    if not name:
        return _NOTES_SYSTEM_PROMPT
    return (_NOTES_SYSTEM_PROMPT + f" The transcript is in {name}: write every "
            f"sentence and bullet in {name}, keeping only the section headings in English.")


async def _generate_notes_async(transcript_text: str, *, owner: Optional[str],
                                language: str = "") -> str:
    from src.task_endpoint import task_llm_call_async
    from src.text_helpers import strip_think

    body = transcript_text[:NOTES_TRANSCRIPT_CHAR_BUDGET]
    out = await task_llm_call_async(
        [
            {"role": "system", "content": _notes_prompt(language)},
            {"role": "user", "content": body},
        ],
        owner=owner,
        temperature=0.2,
    )
    return strip_think(str(out or "")).strip()


def _generate_notes_sync(transcript_text: str, *, owner: Optional[str],
                         language: str = "") -> str:
    import asyncio
    return asyncio.run(_generate_notes_async(transcript_text, owner=owner, language=language))


_FALLBACK_NOTES = (
    "## Summary\n\n_Notes could not be generated automatically. The "
    "transcript below is unedited by a model._\n\n"
    "## Decisions\n\nNone recorded.\n\n"
    "## Action items\n\nNone recorded.\n\n"
    "## Open questions\n\nNone recorded."
)


# ── Job orchestration ───────────────────────────────────────────────────

def create_job(
    audio_bytes: bytes,
    filename: str,
    *,
    title: str = "",
    language: str = "",
    project_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> str:
    """Persist the upload, register a job and start the background thread.
    Returns the job id immediately — the caller polls ``get_job``."""
    _ensure_dirs()
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(_TMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    ext = os.path.splitext(filename or "")[1] or ".webm"
    src_path = os.path.join(job_dir, f"source{ext}")
    Path(src_path).write_bytes(audio_bytes)

    _update_job(
        job_id,
        status="queued",
        owner=owner or "",
        filename=filename or "audio",
        title=title or "",
        project_id=project_id or None,
        created_at=datetime.now(timezone.utc).isoformat(),
        meeting_id=None,
        error=None,
    )

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, src_path, job_dir, filename, title, language, project_id, owner),
        daemon=True,
        name=f"meeting-job-{job_id[:8]}",
    )
    thread.start()
    return job_id


def _run_job(job_id: str, src_path: str, job_dir: str, filename: str, title: str,
             language: str, project_id: Optional[str], owner: Optional[str]) -> None:
    _update_job(job_id, status="running")
    try:
        from services.stt.stt_service import get_stt_service
        from src.settings import get_setting

        stt_service = get_stt_service()
        if not stt_service.available:
            _update_job(job_id, status="failed", error="No STT provider is configured or available.")
            return

        chunk_seconds = int(get_setting("meetings_chunk_seconds", DEFAULT_CHUNK_SECONDS) or DEFAULT_CHUNK_SECONDS)
        chunks = _split_chunks(src_path, job_dir, max(30, chunk_seconds))

        started = time.perf_counter()
        result = _transcribe_chunks(chunks, language=language, stt_service=stt_service)
        segments = result["segments"]
        warnings = list(result["warnings"])
        cleanup_stats = result["cleanup_stats"]
        elapsed = time.perf_counter() - started

        if not segments:
            _update_job(job_id, status="failed",
                        error="Transcription produced no usable speech." + (f" {warnings[0]}" if warnings else ""))
            return

        transcript_text = stt_cleanup.segments_to_text(segments)
        transcript_lines = _transcript_lines(segments)

        notes_body = _FALLBACK_NOTES
        model_ok = True
        try:
            generated = _generate_notes_sync(
                transcript_text, owner=owner,
                language=language or _guess_language(transcript_text))
            if generated:
                notes_body = generated
            else:
                model_ok = False
                warnings.append("The notes model returned an empty response; showing transcript only.")
        except Exception as e:  # noqa: BLE001
            model_ok = False
            warnings.append(f"Notes model unavailable ({e}); showing transcript only.")
            logger.warning("meetings %s: notes generation failed: %s", job_id, e)

        note_title = title.strip() or os.path.splitext(filename or "meeting")[0].strip() or "Meeting"
        date_str = datetime.now().strftime("%Y-%m-%d")
        slug = slugify(note_title)
        meeting_id = f"{date_str}-{slug}"
        base_path = os.path.join(MEETINGS_DIR, meeting_id)
        md_path = base_path + ".md"
        json_path = base_path + ".json"
        n = 2
        while os.path.exists(md_path) or os.path.exists(json_path):
            meeting_id = f"{date_str}-{slug}-{n}"
            base_path = os.path.join(MEETINGS_DIR, meeting_id)
            md_path = base_path + ".md"
            json_path = base_path + ".json"
            n += 1

        duration_s = (segments[-1].get("end") or 0) if segments else 0

        warning_banner = ""
        if not model_ok:
            warning_banner = (
                "> **Notes could not be generated automatically.** "
                "Showing the cleaned transcript only.\n\n"
            )

        markdown = (
            f"# {note_title}\n\n"
            f"{date_str} · source: {filename or 'audio'}\n\n"
            f"{warning_banner}"
            f"{notes_body}\n\n"
            "## Transcript\n\n" + "\n\n".join(transcript_lines) + "\n"
        )
        Path(md_path).write_text(markdown, encoding="utf-8")

        sidecar = {
            "id": meeting_id,
            "title": note_title,
            "date": date_str,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "owner": owner or "",
            "project_id": project_id or None,
            "source_filename": filename or "",
            "duration_seconds": duration_s,
            "language": language or "",
            "stt_provider": stt_service._load_settings().get("stt_provider", ""),
            "model_ok": model_ok,
            "warnings": warnings,
            "cleanup_stats": cleanup_stats,
            "transcribe_seconds": round(elapsed, 2),
            "chunk_count": len(chunks),
            "md_path": os.path.basename(md_path),
        }
        Path(json_path).write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

        _update_job(job_id, status="done", meeting_id=meeting_id, warnings=warnings)
    except Exception as e:  # noqa: BLE001
        logger.error("meetings %s: job failed: %s", job_id, e, exc_info=True)
        _update_job(job_id, status="failed", error=str(e))
    finally:
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass


# ── Read side ────────────────────────────────────────────────────────────

def _read_sidecar(json_path: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception:
        return None


def list_meetings(*, owner: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    _ensure_dirs()
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(Path(MEETINGS_DIR).glob("*.json"), reverse=True)
    except Exception:
        names = []
    for path in names:
        if os.path.abspath(str(path)) == os.path.abspath(JOBS_FILE):
            continue  # the job registry lives in the same folder
        data = _read_sidecar(str(path))
        if not data or "id" not in data:
            continue
        if owner is not None and data.get("owner", "") != (owner or ""):
            continue
        out.append({k: v for k, v in data.items() if k not in ("cleanup_stats",)})
        if len(out) >= limit:
            break
    return out


def get_meeting(meeting_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "", meeting_id or "")
    if not safe_id or safe_id != meeting_id:
        return None
    json_path = os.path.join(MEETINGS_DIR, safe_id + ".json")
    md_path = os.path.join(MEETINGS_DIR, safe_id + ".md")
    data = _read_sidecar(json_path)
    if not data:
        return None
    if owner is not None and data.get("owner", "") != (owner or ""):
        return None
    try:
        data = dict(data)
        data["markdown"] = Path(md_path).read_text(encoding="utf-8")
    except Exception:
        data["markdown"] = ""
    return data
