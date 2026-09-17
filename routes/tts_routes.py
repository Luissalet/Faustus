# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal
from starlette.concurrency import run_in_threadpool
from services.speech_runtime import capabilities
from core.middleware import require_admin
import anyio
import logging

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    format: Literal["audio", "base64"] = "audio"
    use_cache: bool = True
    expected_provider: str = ""
    language: str = Field(default="", max_length=20, pattern=r"^[a-zA-Z-]*$")


class PiperVoiceDownloadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])
    limiter = anyio.CapacityLimiter(1)

    @router.get("/capabilities")
    async def get_capabilities():
        return capabilities(tts_service._load_settings(), "tts")

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return await run_in_threadpool(tts_service.get_stats)
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text"""
        try:
            def synthesize():
                if request.expected_provider and tts_service._load_settings().get("tts_provider") != request.expected_provider:
                    raise HTTPException(409, "Speech provider changed. Review voice settings and try again.")
                if not tts_service.available:
                    raise HTTPException(503, "TTS unavailable. Check the configured provider.")
                kwargs = {"expected_provider": request.expected_provider} if request.expected_provider else {}
                if request.language:
                    kwargs["language"] = request.language
                return tts_service.synthesize(request.text, use_cache=request.use_cache, **kwargs)

            audio_data = await anyio.to_thread.run_sync(synthesize, limiter=limiter)
            if not audio_data:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "Speech synthesis unavailable. Check the provider."}
                )
            
            if request.format == "base64":
                import base64
                audio_b64 = base64.b64encode(audio_data).decode("ascii")
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                from fastapi.responses import JSONResponse
                return JSONResponse({"audio": audio_b64}, headers={"Cache-Control": "no-store"})
            
            else:  # audio format
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                
                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": "inline; filename=speech.mp3" if "mpeg" in mime else "inline; filename=speech.wav"
                    }
                )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": "Synthesis failed. Check the speech provider and retry."}
            )

    @router.get("/piper/voices")
    async def list_piper_voices():
        """Installed Piper voices plus the recommended catalogue (for Settings → Voice)."""
        from services.tts.piper_voice import piper_status
        try:
            return await run_in_threadpool(piper_status)
        except Exception as e:
            logger.error(f"Failed to list Piper voices: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/piper/voices/download")
    async def download_piper_voice(request: Request, body: PiperVoiceDownloadRequest):
        """Downloads a Piper voice (.onnx + .onnx.json) from the public voice
        repository. Admin only — this is an outbound network call."""
        require_admin(request)
        from services.tts.piper_voice import download_voice
        try:
            await run_in_threadpool(download_voice, body.name)
            return {"success": True, "name": body.name}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Piper voice download failed for {body.name!r}: {e}")
            raise HTTPException(status_code=502, detail=f"Voice download failed: {e}")

    @router.post("/piper/install-binary")
    async def install_piper_binary(request: Request):
        """Downloads and installs the Piper engine binary for this platform
        from its GitHub releases. Admin only — this is an outbound network
        call, and the resulting binary is executed by later syntheses."""
        require_admin(request)
        from services.tts.piper_voice import install_binary
        logger.warning("Admin requested Piper engine binary install")
        try:
            path = await run_in_threadpool(install_binary)
            return {"success": True, "path": str(path)}
        except Exception as e:
            logger.error(f"Piper engine install failed: {e}")
            raise HTTPException(status_code=502, detail=f"Engine install failed: {e}")

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            await run_in_threadpool(tts_service.clear_cache)
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
