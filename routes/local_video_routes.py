"""Bounded offline video tools; inputs and results live only for the request."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from importlib.util import find_spec

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from core.middleware import require_admin
from src.upload_limits import read_upload_limited
from services.local_video import DEMUXERS, validate_segments, MAX_DURATION


def _process(data: bytes, suffix: str, payload: dict):
    with tempfile.TemporaryDirectory(prefix="faustus-local-video-", ignore_cleanup_errors=True) as directory:
        source = Path(directory) / ("input"+suffix)
        source.write_bytes(data)
        config = {**payload, "path": str(source)}
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, "-m", "services.local_video"], cwd=str(Path(__file__).resolve().parents[1]),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            output, _ = proc.communicate(json.dumps(config).encode(), timeout=600)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            else:
                proc.kill()
            proc.wait()
            raise ValueError("Offline processing exceeded 10 minutes; try a shorter video")
        if len(output) > 150000:
            raise ValueError("Offline processing returned too much text")
        result = json.loads(output)
        if proc.returncode or result.get("error"):
            raise ValueError(result.get("error", "Offline processing failed"))
        if payload["mode"] == "transcribe":
            return result
        target = Path(directory) / "localized.mp4"
        if not target.is_file() or not 0 < target.stat().st_size <= 128*1024*1024:
            raise ValueError("The rendered video is empty or exceeds 128 MB")
        return target.read_bytes()


def setup_local_video_routes():
    router = APIRouter(prefix="/api/media/local-video", tags=["media"], dependencies=[Depends(require_admin)])
    limiter = anyio.CapacityLimiter(1)

    @router.get("/capabilities")
    def capabilities():
        return {"ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                "whisper": find_spec("faster_whisper") is not None, "system_voice": os.name == "nt",
                "cloud": False, "downloads": False, "max_seconds": MAX_DURATION}

    @router.post("/{mode}")
    async def process(mode: str, file: UploadFile = File(...), language: str = Form("auto"), segments: str = Form("[]", max_length=100000)):
        if mode not in {"transcribe", "dub"} or language not in {"en", "es", "auto"} or mode == "dub" and language == "auto":
            raise HTTPException(400, "Choose transcription or dubbing and a supported language")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in DEMUXERS:
            raise HTTPException(400, "Use an MP4, MOV, WebM or MKV video")
        data = await read_upload_limited(file, 64*1024*1024, "Video")
        if not data:
            raise HTTPException(400, "Choose a non-empty video")
        from src.settings import get_setting
        try:
            rows = validate_segments(json.loads(segments), MAX_DURATION) if mode == "dub" else []
            payload = {"mode": mode, "language": language, "segments": rows, "model": str(get_setting("stt_model", "base"))}
            result = await anyio.to_thread.run_sync(lambda: _process(data, suffix, payload), limiter=limiter)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "Offline video processing failed; check local dependencies and try again") from exc
        if mode == "transcribe":
            return result
        return Response(result, media_type="video/mp4", headers={"Content-Disposition": 'attachment; filename="localized.mp4"', "Cache-Control": "no-store"})
    return router
