"""Streaming voice sessions of Faustus over MCP (task #420).

Same API the Studio voice panel uses (``/api/voice``): discover speech
capabilities without loading a model, open a voice session on a chat, send a
recorded utterance (replay) or text, read transcripts and spoken audio as they
arrive, interrupt the speech, cancel the turn explicitly, close the session.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("voice")

ACTIONS = ["start", "status", "events", "utterance", "say", "interrupt", "cancel_turn", "stop", "list"]
TOOLS = [
    Tool(name="dictation_workspace",
         description="Manage Faustus dictation vocabulary and recoverable text history. Corrections can learn name spellings. Same data and API as Voice settings. Does not type into another app.",
         inputSchema={"type": "object", "required": ["action"], "properties": {
             "action": {"type": "string", "enum": ["vocabulary", "put", "delete_vocabulary", "history", "correct", "delete_history"]},
             "project": {"type": "string"}, "id": {"type": "string"},
             "heard": {"type": "string"}, "replacement": {"type": "string"},
             "text": {"type": "string"}, "learn": {"type": "boolean"},
             "before": {"type": "number"}, "limit": {"type": "integer"}}},
         annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)),
    Tool(name="voice_capabilities",
         description=("Speech recognition and synthesis configured in Faustus and what the streaming voice session "
                      "supports (input formats, partial transcripts, send modes). Never loads a model. "
                      "Capacidades de voz de Faustus."),
         inputSchema={"type": "object", "properties": {}},
         annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False)),
    Tool(name="voice_session",
         description=("Drive a streaming voice session on a Faustus chat. action=start opens one on session_id "
                      "(the chat); utterance sends a recorded clip (audio_path or audio_b64: wav/webm/ogg/mp3 or raw "
                      "PCM16 16 kHz) that is transcribed, filtered and sent ONCE to the ordinary chat turn, whose "
                      "visible answer is spoken sentence by sentence; say sends text the same way; events reads "
                      "transcripts/audio/turn events after a cursor; interrupt stops the speech only (the agent keeps "
                      "working); cancel_turn stops the agent's turn explicitly; stop closes the voice session. With "
                      "wait=true, utterance/say return when the reply's speech ends (or timeout) and can save the "
                      "spoken audio to save_audio_dir. Sesión de voz en streaming sobre un chat."),
         inputSchema={"type": "object", "required": ["action"], "properties": {
             "action": {"type": "string", "enum": ACTIONS},
             "session_id": {"type": "string", "description": "Chat session id (for start)."},
             "voice_session_id": {"type": "string", "description": "Voice session id returned by start."},
             "language": {"type": "string", "description": "auto, es or en (start)."},
             "read_aloud": {"type": "boolean", "description": "Speak the replies (start; default true)."},
             "send_mode": {"type": "string", "enum": ["server", "client"],
                           "description": "server: the session sends final transcripts to the chat (default)."},
             "follow_current": {"type": "boolean", "description": "Also speak a turn already running at start."},
             "audio_path": {"type": "string", "description": "Local audio file for utterance."},
             "audio_b64": {"type": "string", "description": "Base64 audio for utterance."},
             "sample_rate": {"type": "integer", "description": "For raw PCM16 input; default 16000."},
             "text": {"type": "string", "description": "Text for say."},
             "after": {"type": "integer", "description": "Event cursor for events."},
             "timeout": {"type": "number", "description": "Seconds to wait (events: long poll <= 30; wait: <= 600)."},
             "include_audio": {"type": "boolean", "description": "events: include base64 audio."},
             "wait": {"type": "boolean", "description": "utterance/say: wait for the spoken reply to finish."},
             "save_audio_dir": {"type": "string", "description": "With wait: write each spoken chunk to this folder."},
         }},
         annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)),
]


def _headers():
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    internal = os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN", "")
    if internal:
        headers["X-Odysseus-Internal-Token"] = internal
    return headers


def _base():
    return (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")


async def _request(client, method, path, headers=None, api="/api/voice", **kwargs):
    response = await client.request(method, _base() + api + path, headers={**_headers(), **(headers or {})},
                                    **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {"text": response.text[:500]}
    if not response.is_success:
        return {"exit_code": 1, "status_code": response.status_code, "error": payload}
    return payload


async def _wait_reply(client, voice_id, after, timeout, save_dir):
    """Collect events until the reply's speech ends (or the turn ends without speech)."""
    deadline = time.monotonic() + min(max(float(timeout or 180), 1.0), 600.0)
    collected, audio_files, run_id, turn_done = [], [], "", None
    cursor = int(after)
    out_dir = Path(save_dir) if save_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < deadline:
        page = await _request(client, "GET", f"/sessions/{voice_id}/events",
                              params={"after": cursor, "timeout": min(15.0, max(0.5, deadline - time.monotonic())),
                                      "audio": bool(out_dir)})
        if page.get("exit_code"):
            return {"events": collected, "error": page}
        for event in page.get("events", []):
            cursor = event["seq"]
            if event.get("type") == "sent":
                run_id = event.get("run_id") or run_id
            audio_b64 = event.pop("audio_b64", None)
            if out_dir and audio_b64:
                ext = {"audio/mpeg": ".mp3", "audio/ogg": ".ogg"}.get(event.get("mime"), ".wav")
                target = out_dir / f"voice-{event['seq']:05d}{ext}"
                target.write_bytes(base64.b64decode(audio_b64))
                audio_files.append(str(target))
            collected.append(event)
            if event.get("type") == "turn" and event.get("state") in ("done", "failed", "question") and \
                    (not run_id or event.get("run_id") == run_id):
                turn_done = event
            if event.get("type") == "speech_end" and (not run_id or event.get("run_id") == run_id):
                return {"events": collected, "cursor": cursor, "turn": turn_done, "audio_files": audio_files,
                        "complete": True}
            if event.get("type") == "transcript" and event.get("final") and event.get("verdict") not in (None, "ok"):
                return {"events": collected, "cursor": cursor, "audio_files": audio_files, "complete": True}
            if event.get("type") == "closed":
                return {"events": collected, "cursor": cursor, "audio_files": audio_files, "complete": False}
    return {"events": collected, "cursor": cursor, "turn": turn_done, "audio_files": audio_files,
            "complete": False, "timed_out": True}


async def _voice_session(client, a):
    action = a.get("action")
    voice_id = str(a.get("voice_session_id") or "")
    if action == "list":
        return await _request(client, "GET", "/sessions")
    if action == "start":
        body = {"session_id": str(a.get("session_id") or ""), "language": a.get("language") or "auto",
                "read_aloud": a.get("read_aloud", True), "send_mode": a.get("send_mode") or "server",
                "follow_current": bool(a.get("follow_current", False)), "partials": False}
        result = await _request(client, "POST", "/sessions", json=body)
        result.pop("token", None)
        result.pop("stream_url", None)
        return result
    if not voice_id:
        return {"exit_code": 1, "error": "voice_session_id is required"}
    if action == "status":
        return await _request(client, "GET", f"/sessions/{voice_id}")
    if action == "events":
        page = await _request(client, "GET", f"/sessions/{voice_id}/events",
                              params={"after": int(a.get("after") or 0), "timeout": min(float(a.get("timeout") or 0), 30.0),
                                      "audio": bool(a.get("include_audio"))})
        return page
    if action in ("utterance", "say"):
        status = await _request(client, "GET", f"/sessions/{voice_id}")
        if status.get("exit_code"):
            return status
        cursor = int(status.get("last_seq") or 0)
        if action == "utterance":
            if a.get("audio_path"):
                audio = Path(str(a["audio_path"])).read_bytes()
            elif a.get("audio_b64"):
                audio = base64.b64decode(str(a["audio_b64"]))
            else:
                return {"exit_code": 1, "error": "audio_path or audio_b64 is required"}
            result = await _request(client, "POST", f"/sessions/{voice_id}/utterance", content=audio,
                                    params={"sample_rate": int(a.get("sample_rate") or 16000),
                                            "speech_end_at": time.time()},
                                    headers={"Content-Type": "application/octet-stream"})
        else:
            result = await _request(client, "POST", f"/sessions/{voice_id}/say", json={"text": str(a.get("text") or "")})
        if not a.get("wait") or result.get("exit_code"):
            return result
        waited = await _wait_reply(client, voice_id, cursor, a.get("timeout") or 180, a.get("save_audio_dir"))
        final_status = await _request(client, "GET", f"/sessions/{voice_id}")
        return {"result": result, **waited, "metrics": (final_status.get("metrics") or [])[-1:]}
    if action == "interrupt":
        return await _request(client, "POST", f"/sessions/{voice_id}/interrupt")
    if action == "cancel_turn":
        return await _request(client, "POST", f"/sessions/{voice_id}/cancel_turn")
    if action == "stop":
        return await _request(client, "DELETE", f"/sessions/{voice_id}")
    return {"exit_code": 1, "error": f"Unknown action {action!r}"}


@server.list_tools()
async def list_tools():
    return list(TOOLS)


async def _dictation_workspace(client, a):
    from urllib.parse import quote
    action = a.get("action")
    base = {"api": "/api/dictation"}
    if action in ("vocabulary", "history"):
        params = {k: a[k] for k in ("project", "before", "limit") if k in a}
        return await _request(client, "GET", "/" + action, params=params, **base)
    if action == "put":
        return await _request(client, "POST", "/vocabulary", json={k: a.get(k, "") for k in ("heard", "replacement", "project")}, **base)
    entry_id = quote(str(a.get("id") or ""), safe="")
    if not entry_id:
        return {"exit_code": 1, "error": "id is required"}
    if action == "correct":
        return await _request(client, "PATCH", "/history/" + entry_id, json={"text": a.get("text", ""), "learn": a.get("learn", True)}, **base)
    if action in ("delete_vocabulary", "delete_history"):
        return await _request(client, "DELETE", "/" + ("vocabulary" if action == "delete_vocabulary" else "history") + "/" + entry_id, **base)
    return {"exit_code": 1, "error": "Unknown dictation action"}


@server.call_tool()
async def call_tool(name, arguments):
    args = dict(arguments or {})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=120.0), trust_env=False) as client:
            if name == "voice_capabilities":
                payload = await _request(client, "GET", "/capabilities")
            elif name == "voice_session":
                payload = await _voice_session(client, args)
            elif name == "dictation_workspace":
                payload = await _dictation_workspace(client, args)
            else:
                payload = {"exit_code": 1, "error": f"Unknown voice tool {name}"}
    except (httpx.HTTPError, ValueError, OSError) as error:
        payload = {"exit_code": 1, "error": f"Voice API request failed: {type(error).__name__}: {error}"}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
