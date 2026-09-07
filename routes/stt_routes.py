# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from starlette.concurrency import run_in_threadpool
from services.speech_runtime import capabilities
import anyio
import logging

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])
    limiter = anyio.CapacityLimiter(1)

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
    async def transcribe_audio(file: UploadFile = File(...), expected_provider: str = Form(""), language: str = Form("", max_length=20, pattern=r"^[a-zA-Z-]*$")):
        """Transcribe uploaded audio file to text"""
        try:
            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            metadata = {}
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
                if language:
                    kwargs.update(language_override=language, metadata=metadata)
                return stt_service.transcribe(audio_bytes, **kwargs)

            text = await anyio.to_thread.run_sync(transcribe, limiter=limiter)
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
