"""The family's media services, from inside an app: download from a link, speech to text, text to speech.

Three services with one owner each, called through the hub (``hoard_link.family.call``), so the machine has one yt-dlp and
its updater, one Whisper model in memory and one set of voices instead of one per app:

=========================  ===========  ==================================================================
function                   owner        what it asks
=========================  ===========  ==================================================================
:func:`download` ...       ``links``    ``media_download`` / ``media_status`` / ``media_cancel`` / ``media_info`` ...
:func:`transcribe`         ``funes``    ``transcribe_file`` / ``transcribe_status`` / ``transcribe_cancel``
:func:`speak`              ``prospero`` ``voice_tts``
=========================  ===========  ==================================================================

The tool names, arguments and result shapes are the contract in ``docs/commons/services.md``. Standard library only (the local
fallbacks import their heavy dependencies when they run). Nothing here raises: every function returns a dict, and when the work could
not be done ``{"ok": False, "error": "...", "via": "<who>", "kind": "..."}`` — ``via`` is the owner app, or ``"local"`` for the
fallback, and ``kind`` is ``hub_down`` / ``app_down`` / ``app_missing`` / ``tool_missing`` / ``timeout`` / ``auth`` / ``tool_error`` /
``client_error``. A hub that does not answer says ``"hub unreachable"``, like :mod:`hoard_link.fam_notify`.

Configure the app first (``family.configure`` or ``family.install_fastapi``) so the calls carry the app's own token::

    from hoard_link import family, fam_media

    family.configure("lumiere", DATA_DIR)
    got = fam_media.download("https://youtu.be/xyz", format="audio", sections=[[30, 75]], dest_dir=str(WORK))
    if got["ok"]:
        text = fam_media.transcribe(got["path"], language="auto", progress=print)     # funes via the hub, else faster-whisper here
        if text["ok"]:
            print(text["via"], text["language"], text["text"])

Long work follows the waiting rule (:mod:`hoard_link.waiting`): a download or a transcript that is still running when the owner's
150-second wait ends is polled until ``timeout_s``; past it the answer is ``kind: "timeout"`` with the id (``id`` / ``job_id``) so the
caller can ask again (:func:`status`, :func:`transcribe_status`) or cancel.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import _famsvc as _s
from . import family as _f

__all__ = ["download", "status", "cancel", "info", "subtitles", "audio_for_asr", "tools", "transcribe", "transcribe_status",
           "transcribe_cancel", "speak", "speak_bytes", "available", "forget_availability"]

LINKS = _s.OWNERS["media"]
FUNES = _s.OWNERS["stt"]
PROSPERO = _s.OWNERS["tts"]
ACTIVE_DOWNLOAD = ("queued", "downloading", "processing")
MAX_TTS_CHARS = 20000

_FORMATS = {"auto": "auto", "video": "video", "audio": "audio", "image": "image", "mp3": "audio", "mp4": "video", "photo": "image",
            "photos": "image", "images": "image"}


def available(service: str = "media", timeout: float = 1.0) -> bool:
    """True when the hub is up and the owner of ``service`` (``"media"`` = Links, ``"stt"`` = Funes, ``"tts"`` = Prospero) is running.
    Cached for 30 seconds."""
    return _s.available(service, timeout)


def forget_availability() -> None:
    _s.forget_availability()


# ---------------------------------------------------------------------------------------------------------------------
# download (Links)
# ---------------------------------------------------------------------------------------------------------------------

def _sections(value: Any) -> tuple[Optional[list[list[float]]], str]:
    if value is None or value == []:
        return None, ""
    out: list[list[float]] = []
    try:
        pairs = list(value)
        if len(pairs) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in pairs):
            pairs = [pairs]                     # one section given as [start, end]
        for pair in pairs:
            start, end = float(pair[0]), float(pair[1])
            if not (0 <= start < end) or start != start or end != end or end == float("inf"):
                return None, f"bad section {list(pair)!r}: need 0 <= start < end (seconds)"
            out.append([start, end])
    except (TypeError, ValueError, IndexError):
        return None, "sections must be a list of [start_s, end_s] pairs"
    return out, ""


def _download_view(data: Any, error: str = "") -> dict[str, Any]:
    """Links' agent view of a download plus ``path`` (the first file), a consistent ``ok`` and ``via``. Links' own ``kind`` (video,
    audio, image) is renamed ``media_kind``: ``kind`` is the failure vocabulary of the clients."""
    view = dict(data) if isinstance(data, Mapping) else {}
    if "kind" in view:
        view["media_kind"] = view.pop("kind")
    files = [f for f in (view.get("files") or []) if isinstance(f, Mapping) and f.get("path")]
    if not files and view.get("path"):
        files = [{"path": view["path"]}]                       # an owner that answers just {path}
        view.setdefault("status", "done")
    view["files"] = [dict(f) for f in files]
    view["path"] = files[0]["path"] if files else None
    status = str(view.get("status") or "")
    view["ok"] = bool(view.get("ok", status == "done")) and bool(files)
    view["via"] = LINKS
    if status in ACTIVE_DOWNLOAD:
        view.update(ok=False, still_running=True, kind="timeout")
        view["error"] = str(view.get("error") or error or f"still running ({status}); follow it with status(id)")
    elif not view["ok"]:
        view["kind"] = "tool_error"
        view["error"] = str(view.get("error") or error or ("finished without files" if status == "done" else f"download {status or 'failed'}"))
    return view


def _download_done(data: Any) -> bool:
    return not (isinstance(data, Mapping) and str(data.get("status") or "") in ACTIVE_DOWNLOAD)


@_s.never_raises(LINKS)
def download(url: str, *, format: str = "auto", quality: Any = "best", dest_dir: Optional[str] = None,
             sections: Optional[Sequence[Sequence[float]]] = None, max_duration_s: Optional[float] = None, max_height: Optional[int] = None,
             save_link: bool = False, playlist: bool = False, max_items: int = 50, cookies: Optional[str] = "auto", wait: bool = True,
             timeout_s: float = 150.0) -> dict[str, Any]:
    """Download the media behind ``url`` with Links Hoard (yt-dlp, gallery-dl) and say where it is.

    ``format``: ``auto`` (video, or the photos of a post without video), ``video``, ``audio`` (MP3) or ``image``. ``quality``: ``best`` or a
    maximum height (``1080``, ``720``...). ``dest_dir``: absolute folder (default: Links' downloads folder). ``sections``: only these
    ``[start_s, end_s]`` parts of the video. ``max_duration_s`` refuses a longer video; ``max_height`` caps the resolution. ``save_link``
    also puts the URL in Links' library (off by default: an app downloading for itself should not fill the person's reading list).
    ``cookies``: ``"auto"`` or a browser name whose login cookies to use. With ``wait`` (default) this blocks up to ``timeout_s``, polling
    while the download runs; without it, returns at once with the queued download's ``id``.

    Returns Links' view of the download: ``{ok, id, status, path, files: [{path, name, size, kind}], dir, title, uploader, duration,
    total_bytes, via: "links"}``. ``ok`` is true only when it finished with at least one file; ``path`` is the first one. Still running at
    the deadline: ``{ok: False, still_running: True, id, status, progress, kind: "timeout"}`` (the download goes on; :func:`status` follows it)."""
    url = str(url or "").strip()
    if not url:
        return {"ok": False, "error": "url is required", "via": LINKS, "kind": "client_error"}
    secs, bad = _sections(sections)
    if bad:
        return {"ok": False, "error": bad, "via": LINKS, "kind": "client_error"}
    fmt = _FORMATS.get(str(format or "auto").strip().lower(), str(format))
    args = _s.clean_args(url=url, format=fmt, quality=str(quality if quality not in (None, "") else "best"),
                         dir=dest_dir and _s.as_path(dest_dir), dest_dir=dest_dir and _s.as_path(dest_dir), sections=secs,
                         max_duration_s=max_duration_s, max_height=max_height, save_link=bool(save_link), playlist=bool(playlist),
                         max_items=int(max_items), cookies_browser=cookies)
    return _run_download("media_download", args, wait, timeout_s)


def _run_download(tool: str, args: Mapping[str, Any], wait: bool, timeout_s: float) -> dict[str, Any]:
    """``media_download`` / ``media_audio_for_asr`` (same arguments and answer): start, then follow with ``media_status``."""
    if not wait:
        res = _s.call_tool(LINKS, tool, {**args, "wait": False}, timeout_s=30)
        return _download_view(res["data"]) if res["ok"] else _s.public_error(res, LINKS)
    res = _s.run_job(LINKS, tool, {**args, "wait": True}, "media_status", timeout_s=float(timeout_s), id_field="id", id_arg="id",
                     wait_arg="timeout_s", status_wait_arg="wait_s", min_wait=5.0, is_done=_download_done)
    if res["ok"]:
        return _download_view(res["data"])
    out = _s.public_error(res, LINKS)
    if res.get("kind") == "timeout" and isinstance(res.get("data"), Mapping):
        out.update({k: v for k, v in _download_view(res["data"], res["error"]).items() if k not in ("ok", "error", "via", "kind")})
        out["still_running"] = True
    return out


@_s.never_raises(LINKS)
def status(id: str, wait_s: float = 0) -> dict[str, Any]:
    """The state of a download started with :func:`download` (``wait=False`` or past its timeout): same view as :func:`download`.
    ``wait_s`` (0 to 150) blocks until it finishes or the time runs out."""
    wait = _s.clamp_wait(wait_s)
    res = _s.call_tool(LINKS, "media_status", {"id": str(id), "wait_s": int(wait)}, timeout_s=wait + _s.HUB_MARGIN_S)
    return _download_view(res["data"]) if res["ok"] else _s.public_error(res, LINKS)


@_s.never_raises(LINKS)
def cancel(id: str) -> dict[str, Any]:
    """Stop a queued or running download (partial files are cleaned up by Links)."""
    res = _s.call_tool(LINKS, "media_cancel", {"id": str(id)}, timeout_s=30)
    return _download_view(res["data"]) if res["ok"] else _s.public_error(res, LINKS)


def _info_from_probe(probe: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(probe)
    out.setdefault("thumbnail", "")
    out.setdefault("subtitle_langs", [])
    out["partial"] = True          # media_probe knows nothing of thumbnails or subtitle languages
    return out


@_s.never_raises(LINKS)
def info(url: str) -> dict[str, Any]:
    """What a link holds, without downloading: ``{ok, title, description, uploader, duration, thumbnail, subtitle_langs, via}`` (Links'
    ``media_info``; against an older Links that only has ``media_probe`` the same keys come back with ``partial: True`` and no thumbnail or
    subtitle languages). Needs the network on the owner's side."""
    args = {"url": str(url or "").strip()}
    if not args["url"]:
        return {"ok": False, "error": "url is required", "via": LINKS, "kind": "client_error"}
    res = _s.call_tool(LINKS, "media_info", args, timeout_s=120)
    if not res["ok"] and res.get("kind") == "tool_missing":
        res = _s.call_tool(LINKS, "media_probe", args, timeout_s=120)
        if res["ok"] and isinstance(res["data"], Mapping):
            return {"ok": True, **_info_from_probe(res["data"]), "via": LINKS}
    if not res["ok"]:
        return _s.public_error(res, LINKS)
    return {"ok": True, **(dict(res["data"]) if isinstance(res["data"], Mapping) else {}), "via": LINKS}


@_s.never_raises(LINKS)
def subtitles(url: str, langs: Iterable[str] = ("es", "en")) -> dict[str, Any]:
    """The captions of a video as text, without downloading it: ``{ok, text, lang, cues: [{start_s, end_s, text}], via}`` (Links'
    ``media_subtitles``; the first of ``langs`` that exists wins, manual captions before automatic ones). ``ok`` is False with an error
    when the video has none: transcribe its audio instead."""
    wanted = [langs] if isinstance(langs, str) else [str(x) for x in langs]
    res = _s.call_tool(LINKS, "media_subtitles", {"url": str(url or "").strip(), "langs": wanted}, timeout_s=120)
    if not res["ok"]:
        return _s.public_error(res, LINKS)
    data = dict(res["data"]) if isinstance(res["data"], Mapping) else {}
    data.setdefault("text", "")
    data.setdefault("cues", [])
    return {"ok": True, **data, "via": LINKS}


@_s.never_raises(LINKS)
def audio_for_asr(url: str, *, sections: Optional[Sequence[Sequence[float]]] = None, timeout_s: float = 150.0) -> dict[str, Any]:
    """The audio of a video ready for speech to text: ``{ok, path, via}`` (Links' ``media_audio_for_asr``: a mono 16 kHz WAV; it runs like a
    download, so the answer also carries ``id``, ``files``, ``duration`` ... and a slow one is polled until ``timeout_s``). Against an older
    Links the audio is downloaded as MP3 instead (``converted: False``): faster-whisper and Funes read that as well."""
    url = str(url or "").strip()
    if not url:
        return {"ok": False, "error": "url is required", "via": LINKS, "kind": "client_error"}
    secs, bad = _sections(sections)
    if bad:
        return {"ok": False, "error": bad, "via": LINKS, "kind": "client_error"}
    args = _s.clean_args(url=url, sections=secs)
    got = _run_download("media_audio_for_asr", args, True, timeout_s)
    if got.get("ok"):
        return got
    if got.get("kind") == "tool_missing":
        return {**download(url, format="audio", sections=secs, timeout_s=timeout_s), "converted": False}
    return got


@_s.never_raises(LINKS)
def tools(update: bool = False) -> dict[str, Any]:
    """Which of yt-dlp, gallery-dl and ffmpeg Links found (``media_tools``); ``update=True`` also runs the updaters (can take a minute)."""
    res = _s.call_tool(LINKS, "media_tools", {"update": bool(update)}, timeout_s=180 if update else 30)
    return {"ok": True, **(dict(res["data"]) if isinstance(res["data"], Mapping) else {}), "via": LINKS} if res["ok"] else _s.public_error(res, LINKS)


# ---------------------------------------------------------------------------------------------------------------------
# speech to text (Funes, else faster-whisper in this process)
# ---------------------------------------------------------------------------------------------------------------------

_transcribers: dict[str, Any] = {}
_local_lock = threading.Lock()


def _reset_local() -> None:
    """Forget the cached local models (tests, or to free memory)."""
    with _local_lock:
        for t in _transcribers.values():
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        _transcribers.clear()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def _norm_word(word: Any) -> dict[str, Any]:
    w = dict(word) if isinstance(word, Mapping) else {"word": str(word)}
    start, end = w.pop("start", None), w.pop("end", None)
    w["start_s"] = _num(w.get("start_s", start))
    w["end_s"] = _num(w.get("end_s", end))
    if "p" not in w and "probability" in w:
        w["p"] = w.pop("probability")
    w["word"] = str(w.get("word", w.get("text", "")))
    return w


def _norm_segment(seg: Any) -> dict[str, Any]:
    s = dict(seg) if isinstance(seg, Mapping) else {"text": str(seg)}
    start, end = s.pop("start", None), s.pop("end", None)
    s["start_s"] = _num(s.get("start_s", start))
    s["end_s"] = _num(s.get("end_s", end))
    s["text"] = str(s.get("text", "")).strip()
    s["words"] = [_norm_word(w) for w in (s.get("words") or [])]
    return s


def _transcript(data: Mapping[str, Any], via: str) -> dict[str, Any]:
    """A transcript in the shape of :meth:`hoard_link.media.stt.Transcript.as_dict`, whoever made it, plus ``ok`` and ``via``."""
    segments = [_norm_segment(s) for s in (data.get("segments") or [])]
    text = str(data.get("text") or "").strip() or " ".join(s["text"] for s in segments if s["text"]).strip()
    duration = _num(data.get("duration_s", data.get("duration")), 0.0) or (segments[-1]["end_s"] if segments else 0.0)
    out: dict[str, Any] = {
        "ok": True, "language": str(data.get("language") or ""), "language_probability": _num(data.get("language_probability")),
        "duration_s": round(duration, 3), "text": text, "segments": segments, "model": str(data.get("model") or ""),
        "device": str(data.get("device") or ""), "note": str(data.get("note") or ""), "stats": dict(data.get("stats") or {}), "via": via}
    if data.get("job_id"):
        out["job_id"] = data["job_id"]
    return out


def _local_transcribe(path: str, *, language: str, model: Optional[str], word_timestamps: bool, initial_prompt: str, vad: bool,
                      progress: Optional[Callable[[float], None]]) -> dict[str, Any]:
    from .media import stt
    if not stt.available():
        from .errors import missing_dependency
        raise missing_dependency("faster_whisper", "local speech to text")
    size = str(model or os.environ.get("HOARD_WHISPER_MODEL") or "small")
    with _local_lock:
        engine = _transcribers.get(size)
        if engine is None:
            engine = _transcribers[size] = stt.Transcriber(size=size)
    result = engine.transcribe(path, language=None if language in ("", "auto") else language, word_timestamps=word_timestamps, vad=vad,
                               initial_prompt=initial_prompt, progress=progress)
    return _transcript(result.as_dict() if hasattr(result, "as_dict") else dict(result), "local")


def _progress_from(progress: Optional[Callable[[float], None]]) -> Optional[Callable[[Mapping[str, Any]], None]]:
    if progress is None:
        return None

    def on_data(data: Mapping[str, Any]) -> None:
        value = _s.fraction(data.get("progress"))
        if value is not None:
            progress(value)
    return on_data


def transcribe(path: str, *, language: str = "auto", model: Optional[str] = None, word_timestamps: bool = True, initial_prompt: str = "",
               timeout_s: float = 600.0, local_fallback: bool = True, progress: Optional[Callable[[float], None]] = None,
               vad: bool = True) -> dict[str, Any]:
    """Speech to text for an audio or video file on this computer.

    First Funes's Hoard through the hub (``transcribe_file``, then ``transcribe_status`` polling until ``timeout_s``): one Whisper model
    for the whole family, no new session in Funes. When Funes cannot be reached (hub down, app not running, an older Funes without the
    tool) and ``local_fallback`` is set and ``faster_whisper`` is importable, :class:`hoard_link.media.stt.Transcriber` does it here. A
    job that Funes started and that fails, or is still running at the deadline, is *not* repeated locally.

    ``language``: ``"auto"`` or a code (``es``, ``en``...). ``model``: a Whisper size (``small``, ``medium``, ``large-v3``...; the owner's
    default when None). ``initial_prompt`` primes names and jargon. ``progress(fraction)`` is called as the job advances. Returns the
    :class:`~hoard_link.media.stt.Transcript` dict plus ``ok`` and ``via`` (``"funes"`` or ``"local"``): ``{ok, language, language_probability,
    duration_s, text, segments: [{start_s, end_s, text, words: [{start_s, end_s, word, p}]}], model, device, note, stats, via}``.
    Timeout: ``{ok: False, kind: "timeout", job_id, status, via: "funes"}`` (see :func:`transcribe_status`, :func:`transcribe_cancel`)."""
    try:
        src = _s.as_path(path)
        if not src:
            return {"ok": False, "error": "path is required", "via": FUNES, "kind": "client_error"}
        lang = str(language or "auto").strip() or "auto"
        args = _s.clean_args(path=src, language=lang, model=model, word_timestamps=bool(word_timestamps), initial_prompt=str(initial_prompt or ""),
                             vad=bool(vad))
        res = _s.run_job(FUNES, "transcribe_file", args, "transcribe_status", timeout_s=float(timeout_s), on_data=_progress_from(progress))
        if res["ok"]:
            data = res["data"] if isinstance(res["data"], Mapping) else {}
            if str(data.get("status") or "done").lower() in ("done", "completed", "ok"):
                if progress:
                    try:
                        progress(1.0)
                    except Exception:  # noqa: BLE001
                        pass
                return _transcript(data, FUNES)
            err = _s.fail("tool_error", str(data.get("error") or f"transcription {data.get('status')}"), data=dict(data))
            out = _s.public_error(err, FUNES)
            out.update({"status": data.get("status"), "job_id": data.get("job_id")})
            return out
        if local_fallback and _s.is_unavailable(res):
            try:
                return _local_transcribe(src, language=lang, model=model, word_timestamps=word_timestamps, initial_prompt=str(initial_prompt or ""),
                                         vad=vad, progress=progress)
            except Exception as exc:  # noqa: BLE001 - say both reasons
                return {"ok": False, "error": f"{res['error']}; local fallback failed: {type(exc).__name__}: {exc}"[:400], "via": "local",
                        "kind": res["kind"], "hub_error": res["error"]}
        out = _s.public_error(res, FUNES)
        if res.get("kind") == "timeout":
            job = res.get("data") if isinstance(res.get("data"), Mapping) else {}
            out.update({"job_id": res.get("job_id") or job.get("job_id"), "status": job.get("status") or "running", "still_running": True})
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "via": FUNES, "kind": "client_error"}


@_s.never_raises(FUNES)
def transcribe_status(job_id: str, wait_s: float = 0) -> dict[str, Any]:
    """Follow a transcription that :func:`transcribe` left running (``kind: "timeout"``): the transcript dict when done, else
    ``{ok: False, still_running: True, status, job_id}``. ``wait_s`` (0 to 150) blocks until it finishes or the time runs out."""
    wait = _s.clamp_wait(wait_s)
    res = _s.call_tool(FUNES, "transcribe_status", {"job_id": str(job_id), "wait_s": wait}, timeout_s=wait + _s.HUB_MARGIN_S)
    if not res["ok"]:
        return _s.public_error(res, FUNES)
    data = res["data"] if isinstance(res["data"], Mapping) else {}
    state = str(data.get("status") or "done").lower()
    if state in ("done", "completed", "ok"):
        return _transcript(data, FUNES)
    if state in _s.DONE_STATES:
        return {"ok": False, "error": str(data.get("error") or f"transcription {state}"), "via": FUNES, "kind": "tool_error", "status": state, "job_id": job_id}
    return {"ok": False, "error": f"still running ({state})", "via": FUNES, "kind": "timeout", "still_running": True, "status": state, "job_id": job_id,
            "progress": data.get("progress")}


@_s.never_raises(FUNES)
def transcribe_cancel(job_id: str) -> dict[str, Any]:
    """Stop a transcription job (``transcribe_cancel``): ``{ok, status, job_id, via}``."""
    res = _s.call_tool(FUNES, "transcribe_cancel", {"job_id": str(job_id)}, timeout_s=30)
    if not res["ok"]:
        return _s.public_error(res, FUNES)
    data = dict(res["data"]) if isinstance(res["data"], Mapping) else {}
    return {"ok": True, "job_id": job_id, "status": data.get("status") or "cancelled", "via": FUNES}


# ---------------------------------------------------------------------------------------------------------------------
# text to speech (Prospero, else Link.tts)
# ---------------------------------------------------------------------------------------------------------------------

def _tmp_dir(out_dir: Optional[str]) -> Path:
    base = Path(out_dir) if out_dir else Path(os.environ.get("HOARD_HOME") or Path.home() / ".hoard") / "tmp" / "tts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _local_speak(text: str, voice: Optional[str], link: Any, out_dir: Optional[str]) -> dict[str, Any]:
    audio = _s.run_link(link if link is not None else _s.default_link(), "tts", text, voice)
    if not audio:
        raise RuntimeError("the local TTS returned no audio")
    audio = bytes(audio)
    ext = ".mp3" if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3") else ".wav"
    name = "tts-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10] + f"-{int(time.time() * 1000) % 10_000_000}" + ext
    target = _tmp_dir(out_dir) / name
    target.write_bytes(audio)
    return {"ok": True, "path": str(target), "bytes": len(audio), "engine_id": "link", "via": "local", "temp": out_dir is None}


def speak(text: str, *, voice: Optional[str] = None, engine: Optional[str] = None, lang: Optional[str] = None, speed: Optional[float] = None,
          timeout_s: float = 300.0, local_fallback: bool = True, link: Any = None, out_dir: Optional[str] = None) -> dict[str, Any]:
    """Read ``text`` aloud: a WAV file on this computer. Prospero's Hoard through the hub (``voice_tts``) with its voices (a library voice
    id or name, or an engine's own voice id) and engines; when Prospero cannot be reached and ``local_fallback`` is set, ``link.tts`` (a
    :class:`hoard_link.Link`, default one built for this app; needs ``httpx``) does it, writing the audio under ``out_dir`` or
    ``$HOARD_HOME/tmp/tts`` (the fallback ignores ``engine``, ``lang`` and ``speed``).

    ``voice``: library voice (id or name) or the engine's voice id; empty = the best installed engine. ``engine``: an engine id
    (``piper``...). ``lang``: ``es``, ``en``... ``speed``: 1.0 is normal. At most 20000 characters per call. Returns ``{ok, path, bytes,
    engine_id, via}`` (``via``: ``"prospero"`` or ``"local"``; a local file in the default folder also carries ``temp: True``: the caller
    may delete it after use)."""
    try:
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "empty text", "via": PROSPERO, "kind": "client_error"}
        if len(body) > MAX_TTS_CHARS:
            return {"ok": False, "error": f"text too long ({len(body)} characters; at most {MAX_TTS_CHARS} per call: split it)", "via": PROSPERO,
                    "kind": "client_error"}
        args = _s.clean_args(text=body, voice=voice, engine=engine, lang=lang, speed=speed)
        res = _s.call_tool(PROSPERO, "voice_tts", args, timeout_s=float(timeout_s))
        if res["ok"]:
            data = res["data"] if isinstance(res["data"], Mapping) else {}
            path = str(data.get("path") or "")
            if not path:
                return {"ok": False, "error": "Prospero returned no audio path", "via": PROSPERO, "kind": "tool_error"}
            size = data.get("bytes")
            if not isinstance(size, int):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
            return {"ok": True, "path": path, "bytes": size, "engine_id": str(data.get("engine_id") or ""), "via": PROSPERO}
        if local_fallback and _s.is_unavailable(res):
            try:
                return _local_speak(body, voice, link, out_dir)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{res['error']}; local fallback failed: {type(exc).__name__}: {exc}"[:400], "via": "local",
                        "kind": res["kind"], "hub_error": res["error"]}
        return _s.public_error(res, PROSPERO)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "via": PROSPERO, "kind": "client_error"}


def speak_bytes(text: str, **kwargs: Any) -> dict[str, Any]:
    """Like :func:`speak` but returns the audio itself: ``{ok, data: bytes, bytes, engine_id, via, path}``. A temporary local-fallback
    file is removed after reading."""
    res = speak(text, **kwargs)
    if not res.get("ok"):
        return res
    try:
        data = Path(res["path"]).read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read the audio at {res['path']}: {exc.strerror or exc}", "via": res.get("via"), "kind": "client_error"}
    if res.get("temp"):
        try:
            os.unlink(res["path"])
        except OSError:
            pass
    return {**{k: v for k, v in res.items() if k != "temp"}, "data": data, "bytes": len(data)}
