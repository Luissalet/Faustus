"""Streaming voice sessions around the normal chat (task #420).

A voice session is ephemeral and bound to one chat (SID). It does three things,
none of which replaces the chat:

* **Input.** Audio arrives as PCM16 frames over a WebSocket (with the client's
  pre-roll, so the first syllable of a barge-in is not lost) or as one whole
  clip over HTTP/MCP. Partial transcripts are produced while the person speaks
  when asked for; the final transcript is filtered with the same policies as the
  Studio panel (silence hallucinations, stop phrases, optional wake word).
* **Sending.** In ``server`` send mode the final text goes once to the ordinary
  ``/api/chat_stream`` endpoint, called in-process with the session owner (the
  same native call groups use), so tools, memory, project and options are the
  chat's own. In ``client`` mode the client sends it itself (Studio does, so its
  chat view stays the source of truth) and the session only follows the run.
* **Output.** Whatever run is active on the SID -- started here, from Studio,
  from a Hoard panel or by MCP -- is followed through ``agent_runs.subscribe``.
  Only the assistant's visible text is spoken: it is cut into sentences and
  synthesized ahead of playback, so the next sentence is ready while the
  current one plays. ``interrupt`` empties the speech queue and stops speaking
  the current reply; it never cancels the agent's work (``cancel_turn`` is a
  separate, explicit action).

Discovery never loads a model. Sessions live in memory only; audio is not
stored. Nothing here adds a permission, a gate or a log of its own: the turn is
an ordinary chat turn.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import secrets
import time
import unicodedata
import uuid
import wave
import weakref
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Text policies (mirror studio/src/voice/engine.ts) ────────────────────────

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_TABLE_ROW = re.compile(r"^\s*\|.*$", re.M)
_MARKUP = re.compile(r"[*_#>`]")
_SPACES = re.compile(r"\s+")
# Emoji and pictographs: speech engines either skip them or read their names aloud.
_PICTOGRAPHS = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_SPEAKABLE = re.compile(r"[^\W_]", re.UNICODE)


def spoken_text(text: str) -> str:
    """What can be read aloud: no code (an unfinished fence is skipped too), no
    image markup, link text without its URL, no tables, no markdown marks."""
    parts = str(text or "").split("```")
    prose = " ".join(part for index, part in enumerate(parts) if index % 2 == 0)
    prose = _IMAGE.sub("", prose)
    prose = _LINK.sub(r"\1", prose)
    prose = _URL.sub("", prose)
    prose = _TABLE_ROW.sub("", prose)
    prose = _MARKUP.sub("", prose)
    prose = _PICTOGRAPHS.sub("", prose)
    return _SPACES.sub(" ", prose).strip()


_SENTENCE_END = re.compile(r"[.!?。！？](?:\s|$)")
_ABBREVIATION = re.compile(r"\b(?:Dr|Mr|Mrs|Sr|Sra|etc|vs)\.$", re.I)
_CLAUSE_BREAK = re.compile(r"[,;:—](?=\s)")


class SentenceBuffer:
    """Feed the cumulative visible reply; get back the sentences not yet taken.

    The last sentence is held back until the stream ends (its final token may
    still be on its way). ``budget`` caps how much of one reply is spoken; the
    complete answer is always in the chat.
    """

    def __init__(self, budget: int = 4000, first_clause: int = 0) -> None:
        self.budget = budget
        self.cursor = 0
        self.spent = 0
        self.truncated = False
        # >0: the first chunk of a reply may end at a clause break (", ", ": ", "; ")
        # once it has this many characters, so the voice starts before the
        # first full sentence is written.
        self.first_clause = first_clause

    def take(self, text: str, final: bool = False) -> List[str]:
        clean = spoken_text(text)
        out: List[str] = []
        while self.cursor < len(clean) and not self.truncated:
            remaining = clean[self.cursor:].lstrip()
            end = -1
            for match in _SENTENCE_END.finditer(remaining):
                at = match.start() + 1
                if not final and at == len(remaining):
                    break
                if _ABBREVIATION.search(remaining[:at]):
                    continue
                end = at
                break
            if end < 0 and self.first_clause and self.cursor == 0 and not final:
                for match in _CLAUSE_BREAK.finditer(remaining, self.first_clause):
                    if match.start() < 200:
                        end = match.start() + 1
                    break
            if end < 0 and len(remaining) > 320:
                end = remaining.rfind(" ", 0, 280)
            if end < 0 and final:
                end = len(remaining)
            if end < 0:
                break
            part = remaining[:end].strip()
            if self.spent + len(part) > self.budget:
                self.truncated = True
                break
            self.cursor = len(clean) - len(remaining) + end
            self.spent += len(part)
            if part and _SPEAKABLE.search(part):
                out.append(part)
        return out


def normalize_utterance(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE).replace("_", " ")
    return _SPACES.sub(" ", value).strip()


STOP_PHRASES = ("stop", "para", "cállate", "callate", "silencio", "espera", "shut up", "wait")


def is_stop_phrase(text: str, extra: Optional[List[str]] = None) -> bool:
    clean = normalize_utterance(text)
    if not clean:
        return False
    phrases = list(STOP_PHRASES) + [p for p in (extra or []) if isinstance(p, str)]
    return any(normalize_utterance(p) == clean for p in phrases)


_HALLUCINATION_START = re.compile(r"^(subtitulos (?:realizados por|por)|subtitles by|captions by)(?:\s|$)")


def is_hallucination(text: str) -> bool:
    clean = normalize_utterance(text)
    if not clean:
        return True
    if clean == "thanks for watching":
        return True
    return bool(_HALLUCINATION_START.match(clean))


_WAKE_WORDS = ("faustus", "faustos", "fausto", "faust")


def strip_wake_word(text: str) -> Tuple[bool, str]:
    words = str(text or "").split()
    for i in range(min(3, len(words))):
        clean = normalize_utterance(words[i])
        if clean == "faust" and i + 1 < len(words) and normalize_utterance(words[i + 1]) == "us":
            return True, " ".join(words[:i] + words[i + 2:]).strip()
        if clean in _WAKE_WORDS:
            return True, " ".join(words[:i] + words[i + 1:]).strip()
    return False, str(text or "").strip()


_ES_WORDS = {"hola", "gracias", "puedes", "tienes", "quieres", "para", "una", "está", "están", "los", "las", "del",
             "esto", "aquí", "he", "con", "que", "pero", "como", "español"}
_EN_WORDS = {"hello", "thanks", "please", "you", "your", "the", "this", "that", "with", "have", "has", "can", "will",
             "are", "and", "but", "from", "here", "english"}


def speech_language(text: str, fallback: str = "en") -> str:
    """Output voice for a reply ('es' or 'en'): a light heuristic, not an instruction to the model."""
    words = set(re.findall(r"[a-záéíóúüñ]+", spoken_text(text).lower()))
    es = len(words & _ES_WORDS)
    en = len(words & _EN_WORDS)
    if len(words) <= 3 and ({"hello", "thanks"} & words):
        return "en"
    if len(words) <= 3 and ({"hola", "gracias"} & words):
        return "es"
    if es != en and max(es, en) >= 2:
        return "es" if es > en else "en"
    return "es" if str(fallback or "").lower().startswith("es") else "en"


# ── Audio helpers ─────────────────────────────────────────────────────────────

PCM_SAMPLE_RATE = 16000
MAX_UTTERANCE_BYTES = 16 * 1024 * 1024
MAX_PREROLL_MS = 2000


def pcm16_to_wav(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> bytes:
    """Mono little-endian PCM16 -> a WAV container every STT backend can decode."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm[: len(pcm) - (len(pcm) % 2)])
    return buffer.getvalue()


def audio_mime(data: bytes) -> str:
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    return "audio/wav"


def sniff_container(data: bytes) -> str:
    """'wav', 'webm', 'ogg', 'mp4', 'mp3' or '' for raw/unknown bytes."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    return ""


# ── SSE of the ordinary chat run ─────────────────────────────────────────────

def sse_payloads(frame: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in frame.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


class ReplyText:
    """The visible assistant text of one run, as the chat shows it.

    ``delta`` without ``thinking`` appends, ``response_replace`` replaces (the
    editorial pass and recoveries use it). Reasoning, tool output and progress
    events never reach the voice.
    """

    def __init__(self) -> None:
        self.text = ""
        self.replaced = False
        self.terminal: Optional[Dict[str, Any]] = None
        self.question: Optional[Dict[str, Any]] = None
        self.error = ""
        self.done = False

    def feed(self, payload: Dict[str, Any], event_kind: str = "") -> None:
        kind = payload.get("type")
        if kind == "response_replace":
            new = str(payload.get("text") or "")
            if not new.startswith(self.text):
                self.replaced = True
            self.text = new
            return
        if kind == "agent_terminal":
            self.terminal = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            return
        if kind == "ask_user" or payload.get("ask_user"):
            self.question = payload.get("ask_user") or payload.get("data") or payload
            return
        if event_kind == "error" or kind == "error":
            self.error = str(payload.get("error") or payload.get("message") or "The turn failed")
            return
        if payload.get("thinking"):
            return
        if kind in (None, "", "delta") and isinstance(payload.get("delta"), str):
            self.text += payload["delta"]


# ── Sessions ─────────────────────────────────────────────────────────────────

SEND_MODES = ("server", "client")
EVENT_KEEP = 400
AUDIO_KEEP = 60
IDLE_TIMEOUT_S = 15 * 60
MAX_SESSIONS_PER_OWNER = 4
# The first spoken chunk of a reply may end at a clause break once it is this long.
FIRST_CLAUSE_CHARS = 40
# Partial transcripts only when the recognizer is fast enough not to delay the
# final one (a CPU Whisper re-transcribing a growing clip competes with it).
PARTIAL_MAX_STT_MS = 700

# One local transcription and one synthesis at a time across all voice
# sessions: both run on this machine and compete with the chat for it.
_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[asyncio.Lock, asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def _locks() -> Tuple[asyncio.Lock, asyncio.Lock]:
    """(stt, tts) locks of the running event loop (one loop in production; tests start several)."""
    loop = asyncio.get_running_loop()
    pair = _LOCKS.get(loop)
    if pair is None:
        pair = (asyncio.Lock(), asyncio.Lock())
        _LOCKS[loop] = pair
    return pair


class VoiceEvent:
    __slots__ = ("seq", "type", "data", "audio", "at")

    def __init__(self, seq: int, kind: str, data: Dict[str, Any], audio: Optional[bytes]) -> None:
        self.seq = seq
        self.type = kind
        self.data = data
        self.audio = audio
        self.at = time.time()

    def public(self, include_audio: bool = False) -> Dict[str, Any]:
        out = {"seq": self.seq, "type": self.type, "at": self.at, **self.data}
        if self.audio is not None:
            out["audio_bytes"] = len(self.audio)
            if include_audio:
                out["audio_b64"] = base64.b64encode(self.audio).decode("ascii")
        return out


class VoiceTurnError(RuntimeError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


class VoiceSession:
    """One ephemeral voice conversation over one chat. See the module docstring."""

    def __init__(self, manager: "VoiceSessionManager", *, sid: str, owner: Optional[str], language: str = "auto",
                 read_aloud: bool = True, partials: bool = True, send_mode: str = "server",
                 wake_word: bool = False, follow_current: bool = False) -> None:
        self.manager = manager
        self.id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(24)
        self.sid = sid
        self.owner = owner
        self.turn_owner = owner
        self.language = (language or "auto").lower()
        self.read_aloud = bool(read_aloud)
        self.partials = bool(partials)
        self.send_mode = send_mode if send_mode in SEND_MODES else "server"
        self.wake_word = bool(wake_word)
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.state = "idle"
        self.closed = False
        self.generation = 0
        self.events: List[VoiceEvent] = []
        self._seq = 0
        self._cond = asyncio.Condition()
        self._utt: Optional[bytearray] = None
        self._utt_rate = PCM_SAMPLE_RATE
        self._utt_seq = 0
        self._partial_task: Optional[asyncio.Task] = None
        self._spec: Optional[Dict[str, Any]] = None
        self._partial_at = 0.0
        self._partial_bytes = 0
        self._send_seq = 0
        self._sending = 0
        self.heard_language = ""
        self.metrics: Dict[str, Any] = {}
        self.history: List[Dict[str, Any]] = []
        self._pending_metrics: Optional[Dict[str, Any]] = None
        self._run_metrics: Dict[str, Dict[str, Any]] = {}
        self._speak_task: Optional[asyncio.Task] = None
        self._speaking_run = ""
        self._run_done: Dict[str, bool] = {}
        self._skip_runs: set = set()
        self._followed: set = set()
        self._finished_runs: set = set()
        self._tts_queue: "asyncio.Queue[Tuple[int, str, str, str]]" = asyncio.Queue()
        self._tts_busy = False
        self._tasks: List[asyncio.Task] = []
        baseline = manager.runs.get_active_run(sid) if manager.runs else None
        self._baseline_run = "" if follow_current else str(getattr(baseline, "run_id", "") or "")

    # ── lifecycle and events ──

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._follow_loop(), name=f"voice-follow-{self.id[:8]}"))
        self._tasks.append(asyncio.create_task(self._tts_loop(), name=f"voice-tts-{self.id[:8]}"))

    def touch(self) -> None:
        self.last_activity = time.time()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.sid, "state": self.state, "closed": self.closed,
            "language": self.language, "heard_language": self.heard_language, "read_aloud": self.read_aloud,
            "partials": self.partials, "send_mode": self.send_mode, "wake_word": self.wake_word,
            "created_at": self.created_at, "last_activity": self.last_activity, "last_seq": self._seq,
            "speaking_run_id": self._speaking_run, "queued_sentences": self._tts_queue.qsize(),
            "utterances": len(self.history), "metrics": list(self.history[-10:]),
            "stream_path": f"/api/voice/sessions/{self.id}/stream",
        }

    def _set_state(self, state: str) -> None:
        if not self.closed:
            self.state = state

    async def emit(self, kind: str, data: Optional[Dict[str, Any]] = None, audio: Optional[bytes] = None) -> VoiceEvent:
        async with self._cond:
            self._seq += 1
            event = VoiceEvent(self._seq, kind, dict(data or {}), audio)
            self.events.append(event)
            if len(self.events) > EVENT_KEEP:
                del self.events[: len(self.events) - EVENT_KEEP]
            audio_events = [e for e in self.events if e.audio is not None]
            for old in audio_events[:-AUDIO_KEEP]:
                old.audio = None
            self._cond.notify_all()
        self.touch()
        return event

    async def events_after(self, after: int, timeout: float = 0.0) -> List[VoiceEvent]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        async with self._cond:
            while True:
                fresh = [e for e in self.events if e.seq > after]
                if fresh or self.closed:
                    return fresh
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    return []

    async def close(self, reason: str = "closed") -> None:
        if self.closed:
            return
        self.generation += 1
        self._drain_queue()
        await self.emit("closed", {"reason": reason})
        self.closed = True
        self.state = "closed"
        for task in [*self._tasks, self._speak_task, self._partial_task]:
            if task and not task.done():
                task.cancel()
        async with self._cond:
            self._cond.notify_all()

    # ── input ──

    def begin_utterance(self, sample_rate: int = PCM_SAMPLE_RATE, preroll_ms: int = 0) -> int:
        self._utt = bytearray()
        self._utt_rate = int(sample_rate) if 8000 <= int(sample_rate) <= 48000 else PCM_SAMPLE_RATE
        self._utt_seq += 1
        self._spec = None
        self._partial_at = time.monotonic()
        self._partial_bytes = 0
        self.metrics = {"utterance": self._utt_seq, "started": time.time(),
                        "preroll_ms": max(0, min(int(preroll_ms or 0), MAX_PREROLL_MS))}
        self._set_state("listening")
        self.touch()
        return self._utt_seq

    async def add_audio(self, chunk: bytes) -> None:
        if self._utt is None:
            self.begin_utterance()
        assert self._utt is not None
        if len(self._utt) + len(chunk) > MAX_UTTERANCE_BYTES:
            await self.emit("error", {"stage": "input", "message": "The utterance is too long; finish it and start another."})
            return
        self._utt.extend(chunk)
        self.touch()
        min_bytes = int(self._utt_rate * 2 * 0.6)
        if (self.partials and self.manager.partials_affordable()
                and (self._partial_task is None or self._partial_task.done())
                and time.monotonic() - self._partial_at >= 1.0
                and len(self._utt) - self._partial_bytes >= min_bytes):
            self._partial_at = time.monotonic()
            self._partial_bytes = len(self._utt)
            seq = self._utt_seq
            snapshot = bytes(self._utt)
            self._partial_task = asyncio.create_task(self._partial(seq, snapshot))

    async def _partial(self, seq: int, snapshot: bytes) -> None:
        stt_lock, _ = _locks()
        if stt_lock.locked():
            return  # a final transcription is running; never delay it for a guess
        try:
            text, meta = await self._transcribe(snapshot, "pcm16", self._utt_rate)
        except Exception:  # noqa: BLE001 - a failed partial is just a missing preview
            logger.debug("voice partial failed", exc_info=True)
            return
        if seq == self._utt_seq and self._utt is not None and text and not is_hallucination(text):
            await self.emit("transcript", {"final": False, "text": text, "utterance": seq,
                                            "stt_ms": meta.get("server_ms")})

    def speculate(self) -> bool:
        """The client heard a short pause: transcribe what arrived so far now, while it confirms the end of the turn.

        If the person speaks again the client withdraws it (``speculate_cancel``);
        if the turn ends with no voice after the snapshot, that transcript is the
        turn's and the second transcription is saved. Nothing is sent to the chat
        before the end is confirmed.
        """
        if self._utt is None or len(self._utt) < int(self._utt_rate * 2 * 0.15):
            return False
        snapshot = bytes(self._utt)
        seq = self._utt_seq
        task = asyncio.create_task(self._transcribe(snapshot, "pcm16", self._utt_rate))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self._spec = {"seq": seq, "bytes": len(snapshot), "task": task}
        self.metrics["speculations"] = int(self.metrics.get("speculations", 0)) + 1
        return True

    def speculate_cancel(self) -> None:
        if self._spec is not None:
            self.metrics["speculations_withdrawn"] = int(self.metrics.get("speculations_withdrawn", 0)) + 1
        self._spec = None

    async def end_utterance(self, speech_end_at: Optional[float] = None) -> Dict[str, Any]:
        audio = bytes(self._utt or b"")
        self._utt = None
        self.metrics["speech_end"] = float(speech_end_at) if speech_end_at else time.time()
        self.metrics["end_received"] = time.time()
        spec, self._spec = self._spec, None
        if spec is not None and spec["seq"] == self._utt_seq:
            try:
                text, meta = await spec["task"]
            except Exception:  # noqa: BLE001 - fall back to transcribing the whole utterance
                text, meta = None, {}
            if text is not None:
                self.metrics["speculative"] = True
                return await self._finish(audio, "pcm16", self._utt_rate, ready=(text, meta))
        return await self._finish(audio, "pcm16", self._utt_rate)

    async def submit_clip(self, audio: bytes, speech_end_at: Optional[float] = None,
                          sample_rate: int = PCM_SAMPLE_RATE) -> Dict[str, Any]:
        self.begin_utterance(sample_rate)
        self._utt = None
        self.metrics["speech_end"] = float(speech_end_at) if speech_end_at else time.time()
        self.metrics["end_received"] = time.time()
        fmt = "container" if sniff_container(audio) else "pcm16"
        return await self._finish(audio, fmt, sample_rate)

    async def _transcribe(self, audio: bytes, fmt: str, rate: int) -> Tuple[str, Dict[str, Any]]:
        if fmt == "pcm16":
            if len(audio) < int(rate * 2 * 0.15):
                return "", {}
            data = pcm16_to_wav(audio, rate)
        else:
            data = audio
        language = "" if self.language in ("", "auto") else self.language
        meta: Dict[str, Any] = {}
        stt_lock, _ = _locks()
        async with stt_lock:
            started = time.perf_counter()
            text = await asyncio.to_thread(self.manager.transcribe, data, language, meta)
            meta["server_ms"] = int((time.perf_counter() - started) * 1000)
        self.manager.note_stt(meta["server_ms"], len(data))
        return (text or "").strip(), meta

    async def _finish(self, audio: bytes, fmt: str, rate: int,
                      ready: Optional[Tuple[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        seq = self._utt_seq
        self._set_state("transcribing")
        if not audio:
            await self.emit("transcript", {"final": True, "text": "", "utterance": seq, "verdict": "empty"})
            self._set_state("idle")
            return {"verdict": "empty", "text": ""}
        try:
            raw, meta = ready if ready is not None else await self._transcribe(audio, fmt, rate)
        except Exception as exc:  # noqa: BLE001 - reported to the client, the session stays usable
            logger.warning("voice transcription failed: %s", exc)
            await self.emit("error", {"stage": "stt", "message": "Transcription failed. Check the speech provider."})
            self._set_state("idle")
            return {"verdict": "error", "text": ""}
        self.metrics["transcript_ready"] = time.time()
        self.metrics["stt_ms"] = meta.get("server_ms")
        if meta.get("language"):
            self.heard_language = str(meta["language"])
        text = raw
        verdict = "ok"
        if not raw:
            verdict = "empty"
        elif is_hallucination(raw):
            verdict = "hallucination"
        elif is_stop_phrase(raw, self.manager.stop_phrases()):
            verdict = "stop_phrase"
        elif self.wake_word:
            matched, stripped = strip_wake_word(raw)
            if not matched:
                verdict = "no_wake_word"
            else:
                text = stripped
                if not text:
                    verdict = "empty"
        if verdict == "stop_phrase":
            await self.interrupt("stop_phrase")
        result: Dict[str, Any] = {"verdict": verdict, "text": text, "raw": raw, "utterance": seq,
                                  "language": self.heard_language, "stt_ms": meta.get("server_ms")}
        await self.emit("transcript", {"final": True, **result})
        if verdict != "ok":
            self._set_state("idle")
            return result
        record = dict(self.metrics)
        if self.send_mode == "server":
            try:
                result["turn"] = await self.send_text(text, source="voice", metrics=record)
            except VoiceTurnError as exc:
                result["turn_error"] = {"status": exc.status, "detail": exc.detail}
        else:
            self._pending_metrics = record
            self._set_state("thinking")
        return result

    # ── the chat turn ──

    async def send_text(self, text: str, source: str = "text", metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send ``text`` once to the ordinary chat endpoint and follow the run it starts."""
        text = str(text or "").strip()
        if not text:
            raise VoiceTurnError(422, "Nothing to send")
        self._send_seq += 1
        client_message_id = f"voice-{self.id[:12]}-{self._send_seq}"
        record = dict(metrics or {"utterance": None, "source": source})
        record["request_sent"] = time.time()
        self._set_state("thinking")
        self._sending += 1  # the follow loop leaves this run to us, with its timings
        try:
            run_id = await self.manager.start_turn(self.turn_owner, self.sid, text, client_message_id)
        except VoiceTurnError as exc:
            self._sending -= 1
            await self.emit("error", {"stage": "turn", "status": exc.status, "message": str(exc.detail)[:500]})
            self._set_state("idle")
            raise
        except Exception:
            self._sending -= 1
            raise
        self._sending -= 1
        record["run_id"] = run_id
        await self.emit("sent", {"text": text, "run_id": run_id, "client_message_id": client_message_id,
                                 "source": source})
        if run_id and run_id not in self._followed:
            run = self.manager.runs.get_active_run(self.sid)
            self._start_speaking(run if run is not None and getattr(run, "run_id", "") == run_id else None,
                                 run_id, record)
        elif run_id:
            self._run_metrics.setdefault(run_id, record)
        return {"run_id": run_id, "client_message_id": client_message_id}

    async def interrupt(self, reason: str = "user") -> Dict[str, Any]:
        """Stop speaking now: drop queued sentences and the rest of this reply. The agent keeps working."""
        self.generation += 1
        self._drain_queue()
        run_id = self._speaking_run
        if run_id:
            self._skip_runs.add(run_id)
        await self.emit("interrupted", {"reason": reason, "run_id": run_id})
        if self.state == "speaking":
            self._set_state("thinking" if run_id and not self._run_done.get(run_id) else "idle")
        return {"interrupted": True, "run_id": run_id}

    async def cancel_turn(self) -> Dict[str, Any]:
        """Explicitly stop the agent's current turn on this chat (separate from interrupting the voice)."""
        await self.interrupt("cancel_turn")
        stopped = bool(self.manager.runs.stop(self.sid)) if self.manager.runs else False
        await self.emit("turn_cancel", {"stopped": stopped})
        return {"stopped": stopped}

    def _drain_queue(self) -> None:
        while True:
            try:
                self._tts_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ── output: follow runs and speak them ──

    async def _follow_loop(self) -> None:
        runs = self.manager.runs
        while not self.closed:
            try:
                run = runs.get_active_run(self.sid) if runs else None
                run_id = str(getattr(run, "run_id", "") or "") if run is not None else ""
                if (run_id and not self._sending and run_id not in self._followed
                        and run_id != self._baseline_run):
                    self._start_speaking(run, run_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - following is best effort, the chat is unaffected
                logger.debug("voice follow loop", exc_info=True)
            await asyncio.sleep(0.05)

    def _start_speaking(self, run: Any, run_id: str, record: Optional[Dict[str, Any]]) -> None:
        self._followed.add(run_id)
        if record is None and self._pending_metrics is not None:
            record = self._pending_metrics
            self._pending_metrics = None
        record = dict(record or {"utterance": None, "source": "chat"})
        record["run_id"] = run_id
        record.setdefault("run_seen", time.time())
        self._run_metrics[run_id] = record
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        self._speak_task = asyncio.create_task(self._speak_run(run, run_id), name=f"voice-run-{run_id[:8]}")

    async def _speak_run(self, run: Any, run_id: str) -> None:
        reply = ReplyText()
        state = {"buffer": SentenceBuffer(first_clause=FIRST_CLAUSE_CHARS), "spoken": set()}
        record = self._run_metrics.get(run_id, {})
        self._speaking_run = run_id
        self._run_done[run_id] = False
        await self.emit("turn", {"run_id": run_id, "state": "running"})
        self._set_state("thinking")
        pending = ""
        try:
            async for chunk in self.manager.runs.subscribe(self.sid, expected_run=run):
                pending += (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)).replace("\r\n", "\n")
                while "\n\n" in pending:
                    frame, pending = pending.split("\n\n", 1)
                    kind = next((line.split(":", 1)[1].strip().lower() for line in frame.splitlines()
                                 if line.startswith("event:")), "")
                    if "data: [DONE]" in frame:
                        reply.done = True
                    for payload in sse_payloads(frame):
                        reply.feed(payload, kind)
                if reply.text.strip() and "first_text" not in record:
                    record["first_text"] = time.time()
                self._queue_sentences(run_id, reply, state, final=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("voice subscribe failed", exc_info=True)
        self._queue_sentences(run_id, reply, state, final=True)
        outcome = "failed" if reply.error or (reply.terminal or {}).get("failed") else "question" if reply.question else "done"
        self._run_done[run_id] = True
        await self.emit("turn", {"run_id": run_id, "state": outcome, "text": reply.text[-4000:],
                                 "error": reply.error, "question": reply.question is not None,
                                 "spoken_chars": state["buffer"].spent, "truncated": state["buffer"].truncated})
        if self._tts_queue.empty() and not self._tts_busy:
            await self._speech_finished(run_id)

    def _queue_sentences(self, run_id: str, reply: ReplyText, state: Dict[str, Any], final: bool) -> None:
        if not self.read_aloud or run_id in self._skip_runs or self.closed:
            return
        if reply.replaced:
            # The visible answer was rewritten (editorial pass, recovery): start
            # over on the new text, but never say the same sentence twice.
            reply.replaced = False
            state["buffer"] = SentenceBuffer(budget=max(0, 4000 - state["buffer"].spent))
        record = self._run_metrics.get(run_id, {})
        for sentence in state["buffer"].take(reply.text, final=final):
            key = normalize_utterance(sentence)
            if not key or key in state["spoken"]:
                continue
            state["spoken"].add(key)
            record.setdefault("first_sentence", time.time())
            fallback = self.heard_language or (self.language if self.language not in ("", "auto") else "es")
            self._tts_queue.put_nowait((self.generation, run_id, sentence, speech_language(reply.text, fallback)))

    async def _tts_loop(self) -> None:
        _, tts_lock = _locks()
        while not self.closed:
            generation, run_id, text, language = await self._tts_queue.get()
            if generation != self.generation or run_id in self._skip_runs:
                continue
            self._tts_busy = True
            try:
                started = time.perf_counter()
                async with tts_lock:
                    if generation != self.generation or run_id in self._skip_runs:
                        continue
                    audio = await asyncio.to_thread(self.manager.synthesize, text, language)
                synth_ms = int((time.perf_counter() - started) * 1000)
                if generation != self.generation or run_id in self._skip_runs:
                    continue
                if not audio:
                    await self.emit("error", {"stage": "tts", "message": "Speech synthesis unavailable. The answer is in the chat."})
                    continue
                record = self._run_metrics.get(run_id, {})
                if "first_audio_ready" not in record:
                    record["first_audio_ready"] = time.time()
                    record["first_synth_ms"] = synth_ms
                self._set_state("speaking")
                await self.emit("audio", {"run_id": run_id, "text": text, "language": language,
                                          "mime": audio_mime(audio), "synth_ms": synth_ms}, audio=audio)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice synthesis failed: %s", exc)
                await self.emit("error", {"stage": "tts", "message": "Speech synthesis failed. The answer is in the chat."})
            finally:
                self._tts_busy = False
            if self._tts_queue.empty() and self._run_done.get(run_id):
                await self._speech_finished(run_id)

    async def _speech_finished(self, run_id: str) -> None:
        if run_id in self._finished_runs:
            return
        self._finished_runs.add(run_id)
        record = self._run_metrics.get(run_id)
        if record is not None and not record.get("finished"):
            record["finished"] = time.time()
            self.history.append(_derived_metrics(record))
            if len(self.history) > 50:
                del self.history[:-50]
        if self._speaking_run == run_id:
            self._set_state("idle")
        await self.emit("speech_end", {"run_id": run_id})

    def note_played(self, run_id: str, at: Optional[float] = None) -> None:
        """The client started playing audio of ``run_id`` (first time counts)."""
        record = self._run_metrics.get(run_id)
        if record is not None and "first_audio_played" not in record:
            record["first_audio_played"] = float(at) if at else time.time()
            for item in reversed(self.history):
                if item.get("run_id") == run_id:
                    item.update(_derived_metrics(record))
                    break


def _ms(record: Dict[str, Any], start: str, end: str) -> Optional[int]:
    a, b = record.get(start), record.get(end)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return int(round((b - a) * 1000))
    return None


def _derived_metrics(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: record.get(k) for k in ("utterance", "run_id", "source", "stt_ms", "first_synth_ms", "preroll_ms",
                                      "speculative", "speculations", "speculations_withdrawn")
           if record.get(k) is not None}
    out.update({
        "speech_end_to_transcript_ms": _ms(record, "speech_end", "transcript_ready"),
        "transcript_to_request_ms": _ms(record, "transcript_ready", "request_sent"),
        "request_to_first_text_ms": _ms(record, "request_sent", "first_text"),
        "first_text_to_first_audio_ms": _ms(record, "first_text", "first_audio_ready"),
        "speech_end_to_first_audio_ready_ms": _ms(record, "speech_end", "first_audio_ready"),
        "speech_end_to_first_audio_played_ms": _ms(record, "speech_end", "first_audio_played"),
    })
    return {k: v for k, v in out.items() if v is not None}


# ── Manager ──────────────────────────────────────────────────────────────────

StartTurn = Callable[[Optional[str], str, str, str], Awaitable[str]]


class VoiceSessionManager:
    """In-memory registry of voice sessions plus the speech services they share."""

    def __init__(self, *, stt: Any, tts: Any, runs: Any = None, start_turn: Optional[StartTurn] = None,
                 session_check: Optional[Callable[[Optional[str], str], None]] = None) -> None:
        self.stt = stt
        self.tts = tts
        if runs is None:
            from src import agent_runs as runs  # type: ignore[no-redef]
        self.runs = runs
        self._start_turn = start_turn
        self._session_check = session_check
        self.sessions: Dict[str, VoiceSession] = {}
        self.stt_ms_per_second: Optional[float] = None  # recognizer speed seen so far (ms per second of audio)

    def note_stt(self, server_ms: int, wav_bytes: int) -> None:
        seconds = max(0.25, wav_bytes / 32000.0)
        rate = server_ms / seconds
        self.stt_ms_per_second = rate if self.stt_ms_per_second is None else 0.7 * self.stt_ms_per_second + 0.3 * rate

    def partials_affordable(self) -> bool:
        """Partials while speaking only when a 2 s clip transcribes well under PARTIAL_MAX_STT_MS."""
        return self.stt_ms_per_second is not None and self.stt_ms_per_second * 2 < PARTIAL_MAX_STT_MS

    # speech services (called in worker threads)

    def stop_phrases(self) -> List[str]:
        try:
            phrases = self.stt._load_settings().get("voice_stop_phrases") or []
        except Exception:  # noqa: BLE001
            return []
        if isinstance(phrases, str):
            phrases = phrases.split()
        return [p for p in phrases if isinstance(p, str) and p.strip()]

    def transcribe(self, audio: bytes, language: str, metadata: Dict[str, Any]) -> str:
        if not self.stt.available:
            raise RuntimeError("Speech recognition is not available")
        return self.stt.transcribe(audio, language_override=language or "auto", metadata=metadata) or ""

    def synthesize(self, text: str, language: str) -> Optional[bytes]:
        if not self.tts.available:
            return None
        return self.tts.synthesize(text, use_cache=False, language=language)

    async def start_turn(self, owner: Optional[str], sid: str, text: str, client_message_id: str) -> str:
        if self._start_turn is None:
            raise VoiceTurnError(503, "Sending to the chat is not configured on this server")
        return await self._start_turn(owner, sid, text, client_message_id)

    # discovery

    def capabilities(self) -> Dict[str, Any]:
        from services.speech_runtime import capabilities as speech_caps
        stt = speech_caps(self.stt._load_settings(), "stt")
        tts = speech_caps(self.tts._load_settings(), "tts")
        server_stt = stt["configured"] and stt["dependency_installed"] and stt["execution"] in ("local", "endpoint")
        server_tts = tts["configured"] and tts["dependency_installed"] and tts["execution"] in ("local", "endpoint")
        return {
            "stt": stt, "tts": tts,
            "streaming": {
                "available": bool(server_stt),
                "speech_output": bool(server_tts),
                "transport": ["websocket", "http"],
                "input_formats": ["pcm16", "wav", "webm", "ogg", "mp4", "mp3"],
                "pcm_sample_rate": PCM_SAMPLE_RATE,
                "max_preroll_ms": MAX_PREROLL_MS,
                "partial_transcripts": bool(server_stt),
                "send_modes": list(SEND_MODES),
                "interrupt_cancels_turn": False,
            },
            "models_loaded_by_discovery": False,
            "active_sessions": sum(1 for s in self.sessions.values() if not s.closed),
        }

    # registry

    async def create(self, owner: Optional[str], sid: str, **options: Any) -> VoiceSession:
        if not sid:
            raise KeyError("A chat session id is required")
        turn_owner = owner
        if self._session_check:
            turn_owner = self._session_check(owner, sid) or owner
        await self.reap()
        mine = sorted((s for s in self.sessions.values() if s.owner == owner and not s.closed),
                      key=lambda s: s.last_activity)
        while len(mine) >= MAX_SESSIONS_PER_OWNER:
            oldest = mine.pop(0)
            await self.close(oldest.id, "replaced")
        session = VoiceSession(self, sid=sid, owner=owner, **options)
        session.turn_owner = turn_owner
        self.sessions[session.id] = session
        session.start()
        await session.emit("ready", {"session_id": sid, "send_mode": session.send_mode,
                                     "read_aloud": session.read_aloud, "partials": session.partials})
        return session

    def get(self, voice_id: str, owner: Optional[str] = None) -> VoiceSession:
        session = self.sessions.get(str(voice_id or ""))
        if session is None or session.closed or (owner is not None and session.owner != owner):
            raise KeyError("Voice session not found")
        return session

    def get_by_token(self, voice_id: str, token: str) -> VoiceSession:
        session = self.sessions.get(str(voice_id or ""))
        if session is None or session.closed or not token or not secrets.compare_digest(session.token, str(token)):
            raise KeyError("Voice session not found")
        return session

    def list(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        return [s.snapshot() for s in self.sessions.values() if not s.closed and (owner is None or s.owner == owner)]

    async def close(self, voice_id: str, reason: str = "closed") -> bool:
        session = self.sessions.pop(str(voice_id or ""), None)
        if session is None:
            return False
        await session.close(reason)
        return True

    async def reap(self) -> None:
        now = time.time()
        for session in list(self.sessions.values()):
            if session.closed or now - session.last_activity > IDLE_TIMEOUT_S:
                await self.close(session.id, "idle")

    async def close_all(self) -> None:
        for voice_id in list(self.sessions):
            await self.close(voice_id, "shutdown")


def native_turn_starter(app: Any, chat_endpoint: Callable[..., Awaitable[Any]]) -> StartTurn:
    """Start an ordinary chat turn in-process as ``owner`` (the call groups use) and return its run id."""

    async def start(owner: Optional[str], sid: str, text: str, client_message_id: str) -> str:
        from fastapi import HTTPException
        from src.group_orchestrator import native_request
        body = {"session": sid, "message": text, "client_message_id": client_message_id, "mode": "agent"}
        request = native_request(app, owner, body)
        try:
            response = await chat_endpoint(request, initial_response_timeout_s=None)
        except HTTPException as exc:
            raise VoiceTurnError(exc.status_code, exc.detail) from exc
        status = int(getattr(response, "status_code", 200) or 200)
        if status >= 400:
            detail: Any = ""
            body_bytes = getattr(response, "body", b"")
            try:
                detail = json.loads(body_bytes or b"{}")
            except ValueError:
                detail = (body_bytes or b"")[:300].decode("utf-8", "replace")
            raise VoiceTurnError(status, detail)
        return str(response.headers.get("X-Odysseus-Run-Id") or "")

    return start


def session_owner_check(session_manager: Any) -> Callable[[Optional[str], str], Optional[str]]:
    """The voice session must belong to a chat the caller can use (same rule as groups).

    Returns the identity the chat turn runs as: the caller, or the chat's own
    owner when the caller is Faustus's in-process tool layer (its MCP servers)
    or the install runs without login.
    """

    def check(owner: Optional[str], sid: str) -> Optional[str]:
        try:
            session = session_manager.get_session(sid)
        except Exception as exc:  # noqa: BLE001
            raise KeyError("Chat session not found") from exc
        if session is None:
            raise KeyError("Chat session not found")
        stored = getattr(session, "owner", None)
        from src.owner_identity import INTERNAL_TOOL_USER, auth_disabled
        if owner == INTERNAL_TOOL_USER or auth_disabled():
            return stored or owner
        if owner and stored and stored != owner:
            raise KeyError("Chat session not found")
        return owner

    return check


__all__ = [
    "MAX_PREROLL_MS", "PCM_SAMPLE_RATE", "ReplyText", "SentenceBuffer", "VoiceEvent", "VoiceSession",
    "VoiceSessionManager", "VoiceTurnError", "is_hallucination", "is_stop_phrase", "native_turn_starter",
    "normalize_utterance", "pcm16_to_wav", "session_owner_check", "sniff_container", "speech_language",
    "spoken_text", "sse_payloads", "strip_wake_word",
]
