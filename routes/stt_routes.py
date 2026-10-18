# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from pydantic import BaseModel, Field
from typing import Literal
from starlette.concurrency import run_in_threadpool
from services.speech_runtime import capabilities
import anyio
import logging
import time

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)


class DictationEdit(BaseModel):
    text: str = Field(max_length=8000)
    mode: Literal["clean", "revise"] = "clean"
    draft: str = Field(default="", max_length=8000)
    project: str = Field(default="", max_length=200)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])
    limiter = anyio.CapacityLimiter(1)

    @router.post("/polish")
    async def polish_dictation(body: DictationEdit, request: Request):
        from services.stt.natural_dictation import polish
        from src.auth_helpers import effective_user
        if body.mode == "revise" and not body.draft.strip():
            raise HTTPException(422, "A draft is required for spoken revision.")
        identity = effective_user(request)
        result = await polish(body.text, body.mode, body.draft, owner=identity, project=body.project)
        if body.mode == "clean" and body.text.strip():
            from src.dictation_vocabulary import remember
            result["id"] = remember(body.text, result["text"], owner=identity, project=body.project)
        return result

    @router.get("/capabilities")
    async def get_capabilities():
        return capabilities(stt_service._load_settings(), "stt")

    @router.get("/stats")
    async def get_stt_stats():
        """Get STT service statistics"""
        try:
            return await run_in_threadpool(stt_service.get_stats)
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/transcribe")
    async def transcribe_audio(file: UploadFile = File(...), expected_provider: str = Form(""), language: str = Form("", max_length=20, pattern=r"^[a-zA-Z-]*$"),
                               language_hint: Literal["", "es", "en"] = Form("")):
        """Transcribe uploaded audio file to text"""
        try:
            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            metadata = {"language_hint": language_hint} if language_hint else {}
            def transcribe():
                if expected_provider:
                    config = stt_service._load_settings()
                    if config.get("stt_provider") != expected_provider:
                        raise HTTPException(409, "Speech provider changed. Review voice settings and try again.")
                    if expected_provider in ("browser", "disabled"):
                        raise HTTPException(503, "Configure a local or endpoint transcription provider.")
                if not stt_service.available:
                    raise HTTPException(503, "STT unavailable. Check the configured provider.")
                kwargs = {"expected_provider": expected_provider} if expected_provider else {}
                if language or language_hint:
                    kwargs.update(language_override=language or "auto", metadata=metadata)
                return stt_service.transcribe(audio_bytes, **kwargs)

            started = time.perf_counter()
            text = await anyio.to_thread.run_sync(transcribe, limiter=limiter)
            metadata["server_ms"] = int((time.perf_counter() - started) * 1000)
            if text is None:
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Transcription failed"}
                )

            return {"text": text, **metadata}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": "Transcription failed. Check the speech provider and retry."}
            )

    return router
