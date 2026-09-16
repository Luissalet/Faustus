"""adapters/ffmpeg.py — WP10: deterministic local composition over ffmpeg.

`docs/spec/creator/plan/docs/06_ADAPTADORES_MULTIMEDIA.md`, "FFmpeg y
composición determinista": argv built from typed operations, executed with
`subprocess` — no shell, no string interpolation — with a timeout, output in
a bounded temporary, and a finished job inspected (`ffprobe`) before anything
downstream is told it is done.

No binary, no adapter. `describe()` says so (`available=False`) and
`submit()` answers `rejected_before_queue` rather than pretending an argv it
cannot run is a queued job — same discipline
`src.media_backends.comfyui.ComfyUIBackend.probe()` already applies to a
ComfyUI that is not running.

Jobs are SYNCHRONOUS today: `submit()` runs `ffmpeg` to completion (bounded
by `DEFAULT_TIMEOUT_S`) before returning, so `status()`/`collect()`
afterwards read a result that is already final. That is a real, documented
limitation (see the WP10 report): a process kill mid-`ffmpeg` orphans the
job with nothing to reconcile against, which is exactly why `describe()`
reports `supports_reconcile=False` rather than inventing an idempotency this
adapter cannot actually offer (03_ARQUITECTURA_Y_CONTRATOS.md: "el runtime
no inventa idempotencia").
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult, new_job_id,
)
from src.creator.adapters.base import BaseAdapter

NAME = "ffmpeg"
SUPPORTED_TASKS = ("transcode", "trim", "concat")
DEFAULT_TIMEOUT_S = 600
_TAIL_BYTES = 4000

#: In-memory job ledger. Deliberately NOT persisted (see module docstring):
#: an ffmpeg job is local, synchronous and finishes inside `submit()`, so
#: nothing here needs to survive a restart the way an outbox entry does.
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


def _ffprobe_path() -> str:
    return shutil.which("ffprobe") or ""


def _ffmpeg_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, timeout=5, text=True)
        first_line = (out.stdout or "").splitlines()
        return first_line[0] if first_line else ""
    except Exception:  # noqa: BLE001 — a version probe must never crash describe()
        return ""


# ── pure task compiler — shared by plan() and submit() ────────────────────

def _compile(op: str, params: Mapping[str, Any], input_count: int) -> Dict[str, Any]:
    """Validates `op`/`params`/`input_count` with NO filesystem or process
    access, and raises `ValueError` naming exactly what is wrong. The same
    function backs `plan()` (which has no real paths yet) and `submit()`
    (which re-validates rather than trusting a plan object handed back by a
    caller — the same discipline `media_runs.start()` re-checks review
    status rather than trusting an earlier `plan()` call)."""
    if op not in SUPPORTED_TASKS:
        raise ValueError(f"unsupported task {op!r}; ffmpeg adapter supports "
                          f"{', '.join(SUPPORTED_TASKS)}")
    ext = str(params.get("output_ext") or "mp4").lstrip(".")
    if not ext or not ext.isalnum():
        raise ValueError("output_ext must be a plain alphanumeric extension")

    if op == "trim":
        if input_count != 1:
            raise ValueError("trim needs exactly one input")
        start, duration = params.get("start_seconds"), params.get("duration_seconds")
        if start is None or duration is None:
            raise ValueError("trim requires start_seconds and duration_seconds")
        try:
            start, duration = float(start), float(duration)
        except (TypeError, ValueError):
            raise ValueError("start_seconds/duration_seconds must be numbers")
        if start < 0 or duration <= 0:
            raise ValueError("start_seconds must be >= 0 and duration_seconds > 0")
        return {"task": op, "ext": ext, "start_seconds": start, "duration_seconds": duration}

    if op == "transcode":
        if input_count != 1:
            raise ValueError("transcode needs exactly one input")
        return {"task": op, "ext": ext,
                "video_codec": str(params.get("video_codec") or "") or None,
                "audio_codec": str(params.get("audio_codec") or "") or None}

    if op == "concat":
        if input_count < 2:
            raise ValueError("concat needs at least two inputs")
        return {"task": op, "ext": ext}

    raise ValueError(f"unsupported task {op!r}")  # pragma: no cover — SUPPORTED_TASKS guards this


def _argv_for(compiled: Mapping[str, Any], exe: str, ordered_paths: Sequence[str],
              out_path: str, workdir: str) -> List[str]:
    """Real argv from a compiled task and RESOLVED staged paths. No shell,
    no string formatting of a filter graph from raw user text — each field
    is its own argv element."""
    task = compiled["task"]
    if task == "trim":
        return [exe, "-y", "-nostdin", "-ss", str(compiled["start_seconds"]),
                "-i", ordered_paths[0], "-t", str(compiled["duration_seconds"]),
                "-c", "copy", out_path]
    if task == "transcode":
        argv = [exe, "-y", "-nostdin", "-i", ordered_paths[0]]
        if compiled.get("video_codec"):
            argv += ["-c:v", compiled["video_codec"]]
        if compiled.get("audio_codec"):
            argv += ["-c:a", compiled["audio_codec"]]
        if not compiled.get("video_codec") and not compiled.get("audio_codec"):
            argv += ["-c", "copy"]
        argv.append(out_path)
        return argv
    if task == "concat":
        listfile = os.path.join(workdir, "concat_list.txt")
        with open(listfile, "w", encoding="utf-8", newline="\n") as fh:
            for path in ordered_paths:
                # The concat demuxer's OWN escaping (single-quote the path,
                # double any embedded single quote) — a second, filter-
                # specific escaping layered on top of argv already having
                # no shell to escape for at all.
                escaped = path.replace("'", "'\\''")
                fh.write(f"file '{escaped}'\n")
        return [exe, "-y", "-nostdin", "-f", "concat", "-safe", "0",
                "-i", listfile, "-c", "copy", out_path]
    raise ValueError(f"unsupported task {task!r}")  # pragma: no cover


def _sha256_file(path: str) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _ffprobe_inspect(path: str) -> Tuple[bool, str, str]:
    """`(valid, media_type, detail)`. A file ffprobe cannot read, or that
    names zero streams, is NOT a completed output — this is the "collect
    valida hash y rechaza salida corrupta sin registrar" closing criterion."""
    exe = _ffprobe_path()
    if not exe:
        return False, "", "ffprobe not found on PATH; cannot validate output"
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=30, text=True)
    except subprocess.TimeoutExpired:
        return False, "", "ffprobe timed out inspecting the output"
    except Exception as exc:  # noqa: BLE001
        return False, "", f"ffprobe failed: {exc}"
    if out.returncode != 0:
        return False, "", (out.stderr or "ffprobe rejected the output")[:2000]
    try:
        payload = json.loads(out.stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        return False, "", f"ffprobe produced unparsable output: {exc}"
    streams = payload.get("streams") or []
    if not streams:
        return False, "", "ffprobe reports zero streams; the output is empty or corrupt"
    fmt_name = str((payload.get("format") or {}).get("format_name") or "")
    kinds = {s.get("codec_type") for s in streams if isinstance(s, dict)}
    media_type = "video/" + fmt_name.split(",")[0] if "video" in kinds else (
        "audio/" + fmt_name.split(",")[0] if "audio" in kinds else f"application/{fmt_name or 'octet-stream'}")
    return True, media_type, f"{len(streams)} stream(s), format={fmt_name or 'unknown'}"


class FfmpegAdapter(BaseAdapter):
    """One local ffmpeg installation, run as a deterministic composition
    worker — never a place a caller's free-text filter graph is executed."""

    name = NAME

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        exe = _ffmpeg_path()
        available = bool(exe and _ffprobe_path())
        reason = "" if available else (
            "ffmpeg/ffprobe not found on PATH; install ffmpeg to enable this adapter "
            "(faustus does not download or bundle it)")
        return AdapterManifest(
            name=NAME, engine="ffmpeg", version=_ffmpeg_version(exe) if exe else "",
            tasks=SUPPORTED_TASKS, supports_reconcile=False, supports_cancel=True,
            available=available, reason=reason,
            limits={"timeout_seconds": DEFAULT_TIMEOUT_S},
        )

    # ── plan (pure) ────────────────────────────────────────────────────

    def plan(self, op: str, params: Mapping[str, Any],
             inputs: Sequence[str]) -> AdapterPlan:
        try:
            compiled = _compile(op, dict(params or {}), len(inputs))
        except ValueError as exc:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                                detail=str(exc))
        if not _ffmpeg_path():
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("ffmpeg_binary",),
                                detail="ffmpeg is not installed")
        return AdapterPlan(
            ok=True, adapter=NAME, task=op,
            estimated_cost={"seconds": "unknown"},
            engine_plan={"compiled": compiled, "input_order": list(inputs)},
        )

    # ── submit (effect — runs ffmpeg synchronously; see module docstring) ─

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="invalid_plan", detail=plan.detail)
        exe = _ffmpeg_path()
        if not exe:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="ffmpeg_unavailable", detail="ffmpeg is not installed")

        engine_plan = plan.engine_plan or {}
        compiled = engine_plan.get("compiled") or {}
        order = engine_plan.get("input_order") or []
        try:
            ordered_paths = [staging.input_paths[occ_id] for occ_id in order]
        except KeyError as exc:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="input_not_staged",
                                 detail=f"occurrence {exc} was not materialised in staging")

        job_id = new_job_id(NAME)
        out_dir = os.path.join(staging.workdir, job_id)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"output.{compiled.get('ext', 'mp4')}")
        argv = _argv_for(compiled, exe, ordered_paths, out_path, out_dir)

        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "running", "output_path": out_path,
                              "argv": argv, "stderr_tail": "", "started_at": time.time()}
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=DEFAULT_TIMEOUT_S,
                                   cwd=out_dir, shell=False)
        except subprocess.TimeoutExpired:
            with _JOBS_LOCK:
                _JOBS[job_id].update(state="failed", detail="ffmpeg exceeded its timeout")
            return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                 reason="timeout", detail="ffmpeg exceeded its timeout")
        except OSError as exc:
            with _JOBS_LOCK:
                _JOBS[job_id].update(state="failed", detail=str(exc))
            return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                 reason="spawn_failed", detail=str(exc))

        stderr_tail = (proc.stderr or b"")[-_TAIL_BYTES:].decode("utf-8", "replace")
        with _JOBS_LOCK:
            job = _JOBS[job_id]
            if job.get("state") == "cancelled":
                # A cancel() that lost the race with an ffmpeg process that
                # was already finishing: the OUTPUT is real, but the caller
                # asked to stop, so the answer stays `cancelled` — a late
                # result must never overwrite a cancellation (RES08).
                return SubmitResult(job_id=job_id, state="accepted",
                                     detail="job was cancelled before this result landed")
            if proc.returncode == 0 and os.path.exists(out_path):
                job.update(state="completed", returncode=proc.returncode, stderr_tail=stderr_tail)
                return SubmitResult(job_id=job_id, state="accepted")
            job.update(state="failed", returncode=proc.returncode, stderr_tail=stderr_tail)
            return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                 reason="ffmpeg_failed",
                                 detail=stderr_tail or f"ffmpeg exited {proc.returncode}")

    # ── status (query) ────────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return StatusResult(job_id=job_id, state="unknown", detail="no such job")
            return StatusResult(job_id=job_id, state=job.get("state", "unknown"),
                                 detail=str(job.get("detail") or job.get("stderr_tail") or ""))

    # ── cancel (effect) ───────────────────────────────────────────────

    def cancel(self, job_id: str) -> CancelResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return CancelResult(job_id=job_id, outcome="unknown", detail="no such job")
            if job["state"] in ("completed", "failed"):
                # Synchronous by construction (see module docstring): by the
                # time a caller can ask to cancel, `submit()` has already
                # returned a final state. A resultado tardío no vence
                # cancelación in the other direction here — this is the
                # mirror case, a cancel arriving AFTER the result.
                return CancelResult(job_id=job_id, outcome="too_late", detail=job["state"])
            if job["state"] == "cancelled":
                return CancelResult(job_id=job_id, outcome="confirmed", detail="already cancelled")
            job["state"] = "cancelled"
            return CancelResult(job_id=job_id, outcome="accepted", detail="marked cancelled")

    # ── collect (query + bounded local effect) ────────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            return CollectResult(ok=False, detail="no such job")
        if job.get("state") != "completed":
            return CollectResult(ok=False, detail=f"job is {job.get('state')}, not completed")
        source = job.get("output_path") or ""
        if not source or not os.path.exists(source):
            return CollectResult(ok=False, detail="the output file is missing")

        os.makedirs(tmp, exist_ok=True)
        target = os.path.join(tmp, os.path.basename(source))
        if os.path.abspath(source) != os.path.abspath(target):
            shutil.copyfile(source, target)

        sha256, size = _sha256_file(target)
        if size == 0:
            return CollectResult(ok=False, detail="the output file is empty; not registering it")
        valid, media_type, detail = _ffprobe_inspect(target)
        output = CollectedOutput(path=target, sha256=sha256, byte_size=size,
                                  media_type=media_type, valid=valid, detail=detail)
        if not valid:
            # Corrupt output: returned so a caller can inspect/log it, but
            # `ok=False` means production_runs.py must not register it.
            return CollectResult(ok=False, outputs=(output,),
                                  detail=f"output failed inspection: {detail}")
        return CollectResult(ok=True, outputs=(output,), detail=detail)

    # ── reconcile (query) — BaseAdapter's default (== status()) is
    # honest here: supports_reconcile=False means "asking again is all
    # this adapter can do", which is exactly what the default provides. ──


ADAPTER_FACTORY = FfmpegAdapter
