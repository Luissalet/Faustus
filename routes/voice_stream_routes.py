"""Streaming voice sessions (task #420): REST for any client, WebSocket for live audio.

See ``services/voice_stream.py`` for the model and docs/api/voice-stream.md for
the protocol. The chat turn is the ordinary one; these routes only carry audio,
transcripts and speech around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from services.voice_stream import MAX_UTTERANCE_BYTES, VoiceSessionManager, VoiceTurnError

logger = logging.getLogger(__name__)


class VoiceSessionCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    language: str = Field(default="auto", max_length=20, pattern=r"^[a-zA-Z-]*$")
    read_aloud: bool = True
    partials: bool = True
    send_mode: str = Field(default="server", pattern=r"^(server|client)$")
    wake_word: bool = False
    follow_current: bool = False


class VoiceSay(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class VoicePlayed(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)
    at: Optional[float] = None


class VoiceOptions(BaseModel):
    read_aloud: Optional[bool] = None
    partials: Optional[bool] = None
    language: Optional[str] = Field(default=None, max_length=20, pattern=r"^[a-zA-Z-]*$")
    wake_word: Optional[bool] = None


def _owner(request: Request) -> Optional[str]:
    from src.auth_helpers import effective_user
    return effective_user(request)


def _apply_options(session: Any, options: Dict[str, Any]) -> None:
    for key in ("read_aloud", "partials", "wake_word"):
        if options.get(key) is not None:
            setattr(session, key, bool(options[key]))
    if options.get("language"):
        session.language = str(options["language"]).lower()


def setup_voice_stream_routes(manager: VoiceSessionManager) -> APIRouter:
    router = APIRouter(prefix="/api/voice", tags=["voice"])

    def session_for(request: Request, voice_id: str):
        try:
            return manager.get(voice_id, _owner(request))
        except KeyError:
            raise HTTPException(404, "Voice session not found")

    @router.get("/capabilities")
    async def voice_capabilities():
        return await asyncio.to_thread(manager.capabilities)

    @router.get("/sessions")
    async def list_voice_sessions(request: Request):
        return {"sessions": manager.list(_owner(request))}

    @router.post("/sessions")
    async def create_voice_session(body: VoiceSessionCreate, request: Request):
        try:
            session = await manager.create(_owner(request), body.session_id, language=body.language,
                                           read_aloud=body.read_aloud, partials=body.partials,
                                           send_mode=body.send_mode, wake_word=body.wake_word,
                                           follow_current=body.follow_current)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        return {**session.snapshot(), "token": session.token,
                "stream_url": f"/api/voice/sessions/{session.id}/stream?token={session.token}"}

    @router.get("/sessions/{voice_id}")
    async def get_voice_session(voice_id: str, request: Request):
        return session_for(request, voice_id).snapshot()

    @router.get("/sessions/{voice_id}/events")
    async def voice_events(voice_id: str, request: Request, after: int = 0, timeout: float = 0.0, audio: bool = False):
        session = session_for(request, voice_id)
        events = await session.events_after(int(after), min(max(float(timeout), 0.0), 30.0))
        return {"events": [e.public(include_audio=audio) for e in events], "state": session.state,
                "last_seq": events[-1].seq if events else int(after)}

    @router.post("/sessions/{voice_id}/utterance")
    async def voice_utterance(voice_id: str, request: Request):
        """One whole clip (WAV/WebM/Ogg/MP4/MP3, or raw PCM16 at ``sample_rate``): transcribe, filter, send."""
        session = session_for(request, voice_id)
        content_type = request.headers.get("content-type", "")
        speech_end_at: Optional[float] = None
        sample_rate = 16000
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(422, "Send the audio as the 'file' field")
            audio = await upload.read()
            speech_end_at = float(form.get("speech_end_at") or 0) or None
            sample_rate = int(form.get("sample_rate") or 16000)
        else:
            audio = await request.body()
            speech_end_at = float(request.query_params.get("speech_end_at") or 0) or None
            sample_rate = int(request.query_params.get("sample_rate") or 16000)
        if not audio:
            raise HTTPException(422, "Empty audio")
        if len(audio) > MAX_UTTERANCE_BYTES:
            raise HTTPException(413, "Audio too large")
        return await session.submit_clip(audio, speech_end_at, sample_rate)

    @router.post("/sessions/{voice_id}/say")
    async def voice_say(voice_id: str, body: VoiceSay, request: Request):
        """Send typed text through the voice session: the reply is spoken like a voice turn."""
        session = session_for(request, voice_id)
        try:
            return await session.send_text(body.text, source="text")
        except VoiceTurnError as exc:
            raise HTTPException(exc.status if 400 <= exc.status < 600 else 502, exc.detail)

    @router.post("/sessions/{voice_id}/interrupt")
    async def voice_interrupt(voice_id: str, request: Request):
        return await session_for(request, voice_id).interrupt("user")

    @router.post("/sessions/{voice_id}/cancel_turn")
    async def voice_cancel_turn(voice_id: str, request: Request):
        return await session_for(request, voice_id).cancel_turn()

    @router.post("/sessions/{voice_id}/played")
    async def voice_played(voice_id: str, body: VoicePlayed, request: Request):
        session_for(request, voice_id).note_played(body.run_id, body.at)
        return {"ok": True}

    @router.patch("/sessions/{voice_id}")
    async def voice_options(voice_id: str, body: VoiceOptions, request: Request):
        session = session_for(request, voice_id)
        _apply_options(session, body.model_dump())
        return session.snapshot()

    @router.delete("/sessions/{voice_id}")
    async def close_voice_session(voice_id: str, request: Request):
        session_for(request, voice_id)
        await manager.close(voice_id, "closed")
        return {"closed": True}

    @router.websocket("/sessions/{voice_id}/stream")
    async def voice_stream(websocket: WebSocket, voice_id: str):
        try:
            session = manager.get_by_token(voice_id, websocket.query_params.get("token", ""))
        except KeyError:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        try:
            after = int(websocket.query_params.get("after") or 0)
        except ValueError:
            after = 0
        send_lock = asyncio.Lock()
        work: set = set()

        async def pump() -> None:
            cursor = after
            while not session.closed:
                events = await session.events_after(cursor, 15.0)
                if not events:
                    async with send_lock:
                        await websocket.send_json({"type": "keepalive", "state": session.state})
                    continue
                for event in events:
                    cursor = event.seq
                    async with send_lock:
                        await websocket.send_json(event.public())
                        if event.audio is not None:
                            await websocket.send_bytes(event.audio)
            for event in await session.events_after(cursor, 0):
                async with send_lock:
                    await websocket.send_json(event.public())

        def spawn(coro) -> None:
            task = asyncio.create_task(coro)
            work.add(task)
            task.add_done_callback(work.discard)

        async def report(coro) -> None:
            try:
                await coro
            except VoiceTurnError:
                pass  # already emitted as an error event
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice stream action failed: %s", exc)
                await session.emit("error", {"stage": "stream", "message": "The voice action failed."})

        pump_task = asyncio.create_task(pump())
        try:
            while not session.closed:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await session.add_audio(message["bytes"])
                    continue
                try:
                    data = json.loads(message.get("text") or "{}")
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "utterance_start":
                    session.begin_utterance(int(data.get("sample_rate") or 16000), int(data.get("preroll_ms") or 0))
                elif kind == "utterance_end":
                    spawn(report(session.end_utterance(data.get("speech_end_at"))))
                elif kind == "speculate":
                    session.speculate()
                elif kind == "speculate_cancel":
                    session.speculate_cancel()
                elif kind == "utterance_cancel":
                    session._utt = None
                    session._set_state("idle")
                elif kind == "interrupt":
                    spawn(report(session.interrupt(str(data.get("reason") or "barge_in"))))
                elif kind == "cancel_turn":
                    spawn(report(session.cancel_turn()))
                elif kind == "say" and str(data.get("text") or "").strip():
                    spawn(report(session.send_text(str(data["text"]), source="text")))
                elif kind == "played" and data.get("run_id"):
                    session.note_played(str(data["run_id"]), data.get("at"))
                elif kind == "options":
                    _apply_options(session, data)
                elif kind == "stop":
                    await manager.close(session.id, "stopped")
                    break
                elif kind == "ping":
                    async with send_lock:
                        await websocket.send_json({"type": "pong", "state": session.state})
                session.touch()
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            for task in list(work):
                if not task.done():
                    await asyncio.wait({task}, timeout=0.5)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    return router
