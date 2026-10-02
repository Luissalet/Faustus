"""Everything that touches the ffmpeg / ffprobe binaries, once: probing, running with progress and cancellation, PCM and frames,
waveform, loudness, encoder detection, "make it play everywhere", WAV helpers and filter escaping.

::

    from hoard_link.media.ffmpeg import FFmpeg

    ff = FFmpeg()                                   # finds ffmpeg / ffprobe lazily (hoard_link.media.bins.find)
    info = ff.summarize(ff.probe("clip.mov"))        # kind, duration_s, width, height (rotation applied), fps, codecs, channels ...
    ff.run(["-i", "in.mov", "out.mp4"], duration_s=info["duration_s"], progress=lambda f: print(f), cancel=event)
    pcm = ff.extract_pcm("talk.mp4", rate=16000)     # mono s16le bytes

Rules: argument **lists** only; every child has ``CREATE_NO_WINDOW``; a cancelled or timed-out run kills the ffmpeg **tree**
(:func:`hoard_link.proc.kill_tree`); a missing binary raises :class:`~hoard_link.errors.Unavailable`; a failing ffmpeg raises
:class:`FFmpegError` carrying the tail of its log. Times are **seconds** (the Lumiere code used milliseconds).
``numpy`` is optional (only the waveform / PCM helpers use it, lazily, and have a pure-Python fallback).
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import wave
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .. import proc
from ..errors import HoardLinkError
from . import bins

__all__ = [
    "FFmpeg", "FFmpegError", "Cancelled", "needs_transcode", "peaks_from_pcm", "filter_path", "filter_text", "atempo_chain", "seconds",
    "fps_fraction", "MEDIA_EXTENSIONS", "is_media_file", "IMAGE_CODECS", "DEMUXERS",
]

log = logging.getLogger("hoard_link.media.ffmpeg")
Cancelled = proc.Cancelled

MAX_MEDIA_BYTES = 8 * 1024 ** 3
SAFE_PROBE_TIMEOUT_S = 10.0
SAFE_PROBE_OUTPUT = 65536
IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "tiff", "gif", "jpeg2000", "jpegls"}
#: extension -> ffprobe demuxer, for ``probe(safe=True)`` (the format is forced and whitelisted, nothing is guessed)
DEMUXERS = {
    ".mp4": "mov", ".mov": "mov", ".m4v": "mov", ".m4a": "mov", ".webm": "matroska", ".mkv": "matroska", ".mp3": "mp3",
    ".flac": "flac", ".ogg": "ogg", ".opus": "ogg", ".aac": "aac", ".wav": "wav",
}
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
}
_SAFE_ENTRIES = (
    "format=format_name,duration,bit_rate:stream=index,codec_type,codec_name,width,height,channels,sample_rate,avg_frame_rate,"
    "r_frame_rate,duration,pix_fmt,nb_frames,color_space:stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=rotation"
)
_SAMPLE_CODEC = {1: "pcm_u8", 2: "pcm_s16le", 4: "pcm_s32le"}


class FFmpegError(HoardLinkError):
    """ffmpeg / ffprobe ran and failed (or the input was refused). ``code``: ``failed``, ``timeout``, ``invalid_media``,
    ``invalid_path``, ``file_too_large``, ``output_limit``, ``nvenc_unavailable``. ``stderr`` is the tail of its log."""

    def __init__(self, message: str, *, code: str = "failed", returncode: Optional[int] = None, stderr: str = ""):
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.stderr = stderr


def is_media_file(path: "str | Path") -> bool:
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


# ---------------------------------------------------------------- pure helpers --

def seconds(t: float) -> str:
    """``12.5`` -> ``"12.500"`` (ffmpeg's ``-ss`` / ``-t``)."""
    return f"{max(0.0, float(t)):.3f}"


def fps_fraction(fps: float) -> str:
    """30 -> ``"30"``, 29.97 -> ``"30000/1001"``."""
    known = {23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001", 47.952: "48000/1001", 119.88: "120000/1001"}
    for value, frac in known.items():
        if abs(fps - value) < 0.01:
            return frac
    f = Fraction(fps).limit_denominator(1001)
    return f"{f.numerator}/{f.denominator}" if f.denominator != 1 else str(f.numerator)


def filter_path(path: "str | Path") -> str:
    """A path as a filter option value (``subtitles=``, ``lut3d=`` …): forward slashes, the drive colon and quotes escaped, wrapped in
    single quotes. ``C:\\a b\\x.ass`` -> ``'C\\:/a b/x.ass'``."""
    text = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{text}'"


def filter_text(text: str) -> str:
    """Escape a literal for a filtergraph option inside single quotes (``drawtext=text='…'``)."""
    return str(text).replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:").replace("%", "\\%")


MIN_ATEMPO, MAX_ATEMPO, MAX_OVERALL_FACTOR = 0.5, 2.0, 4.0


def atempo_chain(factor: float) -> list[str]:
    """``factor`` (source duration / target duration: > 1 speeds up) as a chain of ``atempo=`` filters, each inside ffmpeg's 0.5–2.0
    range; the factor is clamped to 0.25–4. ``1.0`` gives ``[]``."""
    factor = max(1.0 / MAX_OVERALL_FACTOR, min(MAX_OVERALL_FACTOR, float(factor)))
    if abs(factor - 1.0) < 1e-6:
        return []
    stages: list[float] = []
    remaining = factor
    bound = MAX_ATEMPO if remaining > 1.0 else MIN_ATEMPO
    while remaining > MAX_ATEMPO or remaining < MIN_ATEMPO:
        stages.append(bound)
        remaining /= bound
    stages.append(remaining)
    return [f"atempo={s:.6f}" for s in stages]


def needs_transcode(summary: Mapping[str, Any]) -> bool:
    """The "plays everywhere" rule (from the Links downloader): video must be 8-bit 4:2:0 H.264 and audio AAC or MP3."""
    vc, ac, pix = summary.get("video_codec"), summary.get("audio_codec"), str(summary.get("pix_fmt") or "")
    if summary.get("has_video") and (vc != "h264" or re.search(r"yuv420p(?:10|12)|yuv4[24]{2}p|rgb|gbr", pix, re.I)):
        return True
    if summary.get("has_audio") and ac not in ("aac", "mp3"):
        return True
    return False


def _frame_rate(stream: Mapping[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or "0/0"
        try:
            num, den = str(raw).split("/")
            if int(den) and int(num):
                value = int(num) / int(den)
                if 0 < value < 1000:
                    return round(value, 3)
        except (ValueError, ZeroDivisionError):
            continue
    return 0.0


def peaks_from_pcm(pcm: bytes, buckets: int) -> list[float]:
    """Peak amplitude (0..1) of each of ``buckets`` equal slices of mono s16le PCM. Uses numpy when installed."""
    n = len(pcm) // 2
    if buckets <= 0:
        return []
    if n == 0:
        return [0.0] * buckets
    count = min(buckets, n)
    try:
        import numpy as np  # type: ignore

        a = np.abs(np.frombuffer(pcm[: n * 2], dtype="<i2").astype(np.int32))
        edges = [i * n // count for i in range(count + 1)]
        return [min(1.0, float(a[edges[i]:edges[i + 1]].max()) / 32768.0) for i in range(count)]
    except ImportError:
        pass
    from array import array

    arr = array("h")
    arr.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        arr.byteswap()
    edges = [i * n // count for i in range(count + 1)]
    return [min(1.0, max(map(abs, arr[edges[i]:edges[i + 1]])) / 32768.0) for i in range(count)]


# ---------------------------------------------------------------- FFmpeg --

def _input_arg(path: "str | Path") -> str:
    """A file path as an ffmpeg input: never an option (leading ``-``) and never a protocol (``concat:``, ``http:``)."""
    p = os.fspath(path)
    if p.startswith("-"):
        return "./" + p
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", p):  # one letter is a drive
        return "file:" + p
    return p


class FFmpeg:
    """ffmpeg / ffprobe for one process. ``tools`` (optional) maps ``"ffmpeg"`` / ``"ffprobe"`` to a path, an argv list or a
    :class:`hoard_link.media.bins.Tool`; without it the programs are found with :func:`hoard_link.media.bins.find` on first use."""

    def __init__(self, tools: Optional[Mapping[str, Any]] = None):
        self._given = dict(tools or {})
        self._lock = threading.Lock()
        self._encoders: Optional[frozenset[str]] = None
        self._filters: Optional[frozenset[str]] = None
        self._nvenc: Optional[bool] = None

    # -- the programs ----------------------------------------------------
    def _argv(self, name: str) -> list[str]:
        given = self._given.get(name)
        if given:
            if isinstance(given, bins.Tool):
                return list(given.argv)
            if isinstance(given, (list, tuple)):
                return [str(a) for a in given]
            return [os.fspath(given)]
        tool = bins.find(name)
        if not tool:
            raise bins.tool_missing(name)
        return list(tool.argv)

    @property
    def ffmpeg(self) -> list[str]:
        return self._argv("ffmpeg")

    @property
    def ffprobe(self) -> list[str]:
        return self._argv("ffprobe")

    def available(self) -> bool:
        """True when both programs can be found."""
        try:
            self.ffmpeg, self.ffprobe  # noqa: B018
        except HoardLinkError:
            return False
        return True

    @property
    def version(self) -> str:
        if "ffmpeg" in self._given:
            tool = self._given["ffmpeg"]
            if isinstance(tool, bins.Tool):
                return tool.version
        return bins.find("ffmpeg").version

    # -- probing ---------------------------------------------------------
    def probe(self, path: "str | Path", safe: bool = False, *, timeout: Optional[float] = None) -> dict[str, Any]:
        """ffprobe's JSON (``format`` + ``streams``). ``safe=True`` is for files an agent or an upload named: only local regular
        files up to 8 GiB, ``-protocol_whitelist file``, the demuxer forced and whitelisted by extension, ``-max_alloc``,
        ``-probesize``, ``-max_streams``, a 10 s timeout and a 64 KB output cap."""
        p = os.fspath(path)
        name = os.path.basename(p)
        if safe:
            if "://" in p or p.startswith(("\\\\", "//")) or "\x00" in p:
                raise FFmpegError("Use a local media file, not a URL or a network path.", code="invalid_path")
            try:
                st = os.stat(p)
            except OSError as exc:
                raise FFmpegError(f"The media file is missing or unreadable: {name}", code="invalid_path") from exc
            if not os.path.isfile(p) or st.st_size <= 0:
                raise FFmpegError("Use a non-empty regular media file.", code="invalid_path")
            if st.st_size > MAX_MEDIA_BYTES:
                raise FFmpegError("This file exceeds the 8 GiB inspection limit.", code="file_too_large")
            demuxer = DEMUXERS.get(Path(p).suffix.lower())
            cmd = [*self.ffprobe, "-v", "error", "-max_alloc", "67108864", "-protocol_whitelist", "file"]
            if demuxer:
                cmd += ["-format_whitelist", demuxer, "-f", demuxer]
            cmd += ["-probesize", "5000000", "-analyzeduration", "5000000", "-max_streams", "32"]
            if demuxer == "mov":
                cmd += ["-enable_drefs", "0", "-use_absolute_path", "0"]
            cmd += ["-show_entries", _SAFE_ENTRIES, "-of", "json", "-i", _input_arg(p)]
            limit = timeout or SAFE_PROBE_TIMEOUT_S
        else:
            cmd = [*self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", _input_arg(p)]
            limit = timeout or 60.0
        try:
            done = proc.run(cmd, timeout=limit)
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(f"ffprobe took too long on {name}.", code="timeout") from exc
        if done.returncode != 0:
            raise FFmpegError(f"Not a media file ffprobe can read: {name} ({proc.tail_lines(done.stderr, 4, 300)})",
                              code="invalid_media", returncode=done.returncode, stderr=done.stderr)
        if safe and len(done.stdout) > SAFE_PROBE_OUTPUT:
            raise FFmpegError("Media metadata exceeded the output limit.", code="output_limit")
        try:
            info = json.loads(done.stdout or "{}")
        except ValueError as exc:
            raise FFmpegError("ffprobe returned invalid metadata.", code="invalid_media") from exc
        if not isinstance(info, dict):
            raise FFmpegError("ffprobe returned invalid metadata.", code="invalid_media")
        return info

    @staticmethod
    def summarize(info: Mapping[str, Any]) -> dict[str, Any]:
        """The fields an app stores for a media file: ``kind`` (video | audio | image | unknown), ``duration_s`` / ``duration_ms``, display
        ``width`` / ``height`` (a 90/270 degree rotation swaps them), ``fps``, codecs, ``pix_fmt``, ``sample_rate``, ``channels``,
        ``rotation``, ``bit_rate``, ``format``, ``audio_streams``."""
        streams = info.get("streams") or []
        fmt = info.get("format") or {}
        video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = 0.0
        for raw in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
            try:
                value = float(raw)
                if math.isfinite(value):
                    duration = max(duration, value)
            except (TypeError, ValueError):
                continue
        codec = (video or {}).get("codec_name")
        try:
            frames = int((video or {}).get("nb_frames") or 1)
        except ValueError:
            frames = 1
        is_image = bool(video) and not audio and codec in IMAGE_CODECS and (duration < 0.5 or frames <= 1)
        width, height = int((video or {}).get("width") or 0), int((video or {}).get("height") or 0)
        rotation = 0
        for side in (video or {}).get("side_data_list") or []:
            if "rotation" in side:
                try:
                    rotation = int(float(side["rotation"])) % 360
                except (TypeError, ValueError):
                    pass
        tags_rot = ((video or {}).get("tags") or {}).get("rotate")
        if tags_rot:
            try:
                rotation = int(tags_rot) % 360
            except ValueError:
                pass
        if rotation in (90, 270):
            width, height = height, width
        kind = "image" if is_image else ("video" if video else ("audio" if audio else "unknown"))
        return {
            "kind": kind,
            "duration_s": 0.0 if kind == "image" else round(duration, 3),
            "duration_ms": 0 if kind == "image" else int(round(duration * 1000)),
            "width": width, "height": height,
            "fps": _frame_rate(video) if video and kind == "video" else 0.0,
            "has_video": bool(video), "has_audio": bool(audio),
            "video_codec": codec, "audio_codec": (audio or {}).get("codec_name"),
            "pix_fmt": (video or {}).get("pix_fmt"), "color_space": (video or {}).get("color_space") or "",
            "sample_rate": int((audio or {}).get("sample_rate") or 0), "channels": int((audio or {}).get("channels") or 0),
            "rotation": rotation, "bit_rate": int(fmt.get("bit_rate") or 0), "format": fmt.get("format_name"),
            "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        }

    def duration(self, path: "str | Path") -> float:
        """Duration in seconds (``0.0`` when the container does not say). Raises :class:`FFmpegError` for an unreadable file."""
        cmd = [*self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", _input_arg(path)]
        try:
            done = proc.run(cmd, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError("ffprobe took too long.", code="timeout") from exc
        if done.returncode != 0:
            raise FFmpegError(f"Not a media file ffprobe can read: {os.path.basename(os.fspath(path))}", code="invalid_media",
                              returncode=done.returncode, stderr=done.stderr)
        try:
            value = float((done.stdout or "").strip().splitlines()[0])
            return value if math.isfinite(value) else 0.0
        except (ValueError, IndexError):
            return float(self.summarize(self.probe(path))["duration_s"])

    # -- running ---------------------------------------------------------
    def run(self, args: Sequence[Any], *, duration_s: float = 0.0, progress: Optional[Callable[[float], None]] = None,
            cancel: Optional[threading.Event] = None, timeout: Optional[float] = None, cwd: Any = None,
            low_priority: bool = False) -> str:
        """Run ffmpeg with ``-y -loglevel error -progress pipe:1`` and return the stderr tail.

        ``progress(fraction)`` gets 0..1 while it runs (needs ``duration_s``) and 1.0 at the end. Setting ``cancel`` kills the whole
        ffmpeg tree and raises :class:`Cancelled`; ``timeout`` raises :class:`FFmpegError` (``code="timeout"``); a non-zero exit raises
        :class:`FFmpegError` with the log tail.
        """
        cmd = [*self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               *(os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args)]
        log.debug("ffmpeg %s", " ".join(cmd[1:]))
        err: deque[str] = deque(maxlen=400)
        last = [-1.0]

        def on_out(line: str) -> None:
            if progress and duration_s > 0 and (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
                try:  # both keys are microseconds (out_time_ms is a historical misnomer)
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    return
                frac = max(0.0, min(1.0, us / 1_000_000 / duration_s))
                if frac != last[0]:
                    last[0] = frac
                    progress(frac)

        try:
            code = proc.run_streaming(cmd, on_out, stderr_line=err.append, timeout=timeout, cancel=cancel, cwd=cwd, low_priority=low_priority)
        except subprocess.TimeoutExpired:
            raise FFmpegError(f"ffmpeg did not finish within {timeout:g} s.", code="timeout", stderr="\n".join(err)) from None
        text = "\n".join(err)
        if code != 0:
            raise FFmpegError(f"ffmpeg failed ({code}): {proc.tail_lines(text)}", returncode=code, stderr=text)
        if progress and last[0] < 1.0:
            progress(1.0)
        return text

    def capture(self, args: Sequence[Any], *, binary: bool = False, timeout: float = 3600.0,
                cancel: Optional[threading.Event] = None) -> "tuple[Any, str]":
        """Run ffmpeg and return ``(stdout, stderr_text)``: raw PCM / frames (``binary=True``) or analysis filters that print to the log."""
        cmd = [*self.ffmpeg, "-hide_banner", "-nostdin", *(os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args)]
        try:
            done = proc.run(cmd, timeout=timeout, cancel=cancel, text=False)
        except subprocess.TimeoutExpired:
            raise FFmpegError("ffmpeg took too long.", code="timeout") from None
        err_text = (done.stderr or b"").decode("utf-8", "replace")
        if done.returncode != 0:
            raise FFmpegError(f"ffmpeg failed ({done.returncode}): {proc.tail_lines(err_text)}", returncode=done.returncode, stderr=err_text)
        return (done.stdout if binary else done.stdout.decode("utf-8", "replace")), err_text

    # -- extraction ------------------------------------------------------
    def extract_pcm(self, path: "str | Path", *, rate: int = 16000, start_s: float = 0.0, duration_s: float = 0.0, stream: int = 0,
                    cancel: Optional[threading.Event] = None) -> bytes:
        """Mono signed 16-bit little-endian PCM of one audio stream."""
        args: list[Any] = ["-loglevel", "error"]
        if start_s:
            args += ["-ss", seconds(start_s)]
        args += ["-i", _input_arg(path)]
        if duration_s:
            args += ["-t", seconds(duration_s)]
        args += ["-map", f"0:a:{stream}", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        out, _ = self.capture(args, binary=True, cancel=cancel)
        return out

    def extract_frames(self, path: "str | Path", *, width: int, fps: float, start_s: float = 0.0, duration_s: float = 0.0,
                       gray: bool = False, cancel: Optional[threading.Event] = None) -> "tuple[bytes, int, int]":
        """Raw frames (``rgb24`` or ``gray``) scaled to ``width`` at ``fps``: ``(bytes, width, height)``."""
        args: list[Any] = ["-loglevel", "error"]
        if start_s:
            args += ["-ss", seconds(start_s)]
        args += ["-i", _input_arg(path)]
        if duration_s:
            args += ["-t", seconds(duration_s)]
        info = self.summarize(self.probe(path))
        w = int(width)
        h = max(2, int(round(info["height"] * w / max(1, info["width"]) / 2)) * 2) if info["width"] else w
        args += ["-an", "-vf", f"fps={fps},scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "gray" if gray else "rgb24", "pipe:1"]
        out, _ = self.capture(args, binary=True, cancel=cancel)
        return out, w, h

    def grab_frame(self, path: "str | Path", at_s: float, out: "str | Path", width: int = 0) -> Path:
        """One frame at ``at_s`` as an image (PNG / JPEG by the extension of ``out``)."""
        dest = Path(out)
        args: list[Any] = ["-ss", seconds(at_s), "-i", _input_arg(path), "-frames:v", "1"]
        if width:
            args += ["-vf", f"scale={int(width)}:-2"]
        if dest.suffix.lower() in (".jpg", ".jpeg"):
            args += ["-q:v", "3"]
        args.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.run(args, timeout=60)
        return dest

    def waveform_peaks(self, path: "str | Path", buckets: int = 800, *, stream: int = 0, rate: int = 8000) -> list[float]:
        """``buckets`` peak amplitudes (0..1) of the audio, for drawing a waveform."""
        return peaks_from_pcm(self.extract_pcm(path, rate=rate, stream=stream), buckets)

    # -- loudness --------------------------------------------------------
    def loudness(self, path: "str | Path", *, stream: int = 0) -> dict[str, Optional[float]]:
        """EBU R128 measurement (``ebur128`` filter): ``integrated_lufs``, ``lra`` (LU) and ``true_peak_dbfs``; ``None`` for a value
        ffmpeg printed as ``-inf`` (digital silence)."""
        _, err = self.capture(["-nostats", "-i", _input_arg(path), "-map", f"0:a:{stream}", "-af", "ebur128=peak=true:framelog=quiet",
                               "-f", "null", "-"])
        tail = err[err.rfind("Summary:"):] if "Summary:" in err else err

        def grab(label: str, unit: str) -> Optional[float]:
            m = re.search(rf"{label}:\s+(-?inf|-?\d+(?:\.\d+)?)\s+{unit}", tail)
            if not m or "inf" in m.group(1):
                return None
            return float(m.group(1))

        return {"integrated_lufs": grab("I", "LUFS"), "lra": grab("LRA", "LU"), "true_peak_dbfs": grab("Peak", "dBFS")}

    def loudnorm_measure(self, path: "str | Path", *, target_i: float = -16.0, target_tp: float = -1.5, target_lra: float = 11.0,
                         stream: int = 0) -> dict[str, float]:
        """First pass of two-pass ``loudnorm``: ``input_i``, ``input_tp``, ``input_lra``, ``input_thresh``, ``target_offset``.
        Feed the result to :func:`loudnorm_second_pass`."""
        flt = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
        _, err = self.capture(["-nostats", "-i", _input_arg(path), "-map", f"0:a:{stream}", "-af", flt, "-f", "null", "-"])
        measurements = re.findall(r"\{[^{}]*\}", err, re.S)
        raw = next((json.loads(block) for block in reversed(measurements) if '"input_i"' in block and '"target_offset"' in block), None)
        if raw is None:
            raise FFmpegError("loudnorm printed no measurement.", code="failed", stderr=proc.tail_lines(err))
        out: dict[str, float] = {}
        for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
            try:
                out[key] = float(raw[key])
            except (KeyError, TypeError, ValueError):
                out[key] = float("nan")
        return out

    @staticmethod
    def loudnorm_second_pass(measured: Mapping[str, float], *, target_i: float = -16.0, target_tp: float = -1.5,
                             target_lra: float = 11.0) -> str:
        """The ``loudnorm=…`` filter string for the second pass, built from :meth:`loudnorm_measure`'s result."""
        return (f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
                f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}"
                ":linear=true:print_format=summary")

    # -- capabilities ----------------------------------------------------
    def _list(self, kind: str, pattern: str, index: int) -> frozenset[str]:
        done = proc.run([*self.ffmpeg, "-hide_banner", f"-{kind}"], timeout=20)
        names: set[str] = set()
        for line in (done.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(pattern, parts[0]) and (kind != "filters" or "->" in line):
                names.add(parts[index])
        return frozenset(names)

    @property
    def encoders(self) -> frozenset[str]:
        with self._lock:
            if self._encoders is None:
                self._encoders = self._list("encoders", r"[VAS][F.][S.][X.][B.][D.]", 1)
            return self._encoders

    @property
    def filters(self) -> frozenset[str]:
        with self._lock:
            if self._filters is None:
                self._filters = self._list("filters", r"[TSC.]{2,3}", 1)
            return self._filters

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    def _nvenc_works(self) -> bool:
        if "h264_nvenc" not in self.encoders:
            return False
        with self._lock:
            if self._nvenc is None:
                try:
                    done = proc.run([*self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                                     "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
                    self._nvenc = done.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    self._nvenc = False
            return bool(self._nvenc)

    def video_encoder(self, preference: str = "auto", codec: str = "h264") -> str:
        """``h264_nvenc`` / ``hevc_nvenc`` / ``av1_nvenc`` when the GPU encoder really works (tested once), else ``libx264`` / ``libx265`` /
        ``libsvtav1``. ``preference="x264"`` forces software; ``"nvenc"`` raises :class:`FFmpegError` (``nvenc_unavailable``) when it does not work."""
        nv = {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"}[codec]
        sw = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}[codec]
        if preference == "x264":
            return sw
        if nv in self.encoders and self._nvenc_works():
            return nv
        if preference == "nvenc":
            raise FFmpegError("The NVIDIA encoder was requested but it does not work here.", code="nvenc_unavailable")
        return sw

    # -- conversions -----------------------------------------------------
    def _convert(self, src: "str | Path", dst: "str | Path", codec_args: Sequence[str], *, cancel: Optional[threading.Event] = None,
                 progress: Optional[Callable[[float], None]] = None) -> Path:
        out = Path(dst)
        out.parent.mkdir(parents=True, exist_ok=True)
        dur = 0.0
        if progress:
            try:
                dur = self.duration(src)
            except FFmpegError:
                dur = 0.0
        self.run(["-i", _input_arg(src), "-vn", *codec_args, out], duration_s=dur, progress=progress, cancel=cancel)
        return out

    def to_wav16k_mono(self, src: "str | Path", dst: "str | Path", *, cancel: Optional[threading.Event] = None,
                       progress: Optional[Callable[[float], None]] = None) -> Path:
        """16 kHz mono 16-bit WAV (what speech models read)."""
        return self._convert(src, dst, ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"], cancel=cancel, progress=progress)

    def to_mp3(self, src: "str | Path", dst: "str | Path", *, bitrate: str = "192k", cancel: Optional[threading.Event] = None,
               progress: Optional[Callable[[float], None]] = None) -> Path:
        return self._convert(src, dst, ["-c:a", "libmp3lame", "-b:a", bitrate], cancel=cancel, progress=progress)

    def to_ogg_opus(self, src: "str | Path", dst: "str | Path", *, bitrate: str = "64k", cancel: Optional[threading.Event] = None,
                    progress: Optional[Callable[[float], None]] = None) -> Path:
        return self._convert(src, dst, ["-c:a", "libopus", "-b:a", bitrate, "-vbr", "on"], cancel=cancel, progress=progress)

    def ensure_playable(self, path: "str | Path", *, replace: bool = True, crf: int = 20, preset: str = "medium",
                        cancel: Optional[threading.Event] = None, progress: Optional[Callable[[float], None]] = None) -> Path:
        """Make a video play everywhere (the Links downloader's rule): 8-bit 4:2:0 H.264 + AAC/MP3 in an MP4 with ``+faststart``.

        Returns ``path`` untouched when it already qualifies, is not a video, or the conversion fails (a failed conversion never loses the
        original). Otherwise converts to a temporary file and returns the new ``.mp4``: with ``replace=True`` it takes the original's
        place (the source is deleted), with ``replace=False`` it is written next to it as ``<name>.h264.mp4``. Cancelling raises
        :class:`Cancelled` and removes the partial file.
        """
        src = Path(path)
        try:
            info = self.summarize(self.probe(src))
        except FFmpegError:
            return src
        if info["kind"] != "video" or not needs_transcode(info):
            return src
        temp = src.with_name(f"{src.stem}.hoard-tmp.mp4")
        try:
            self.run(["-i", _input_arg(src), "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-b:a", "192k", "-movflags", "+faststart", temp], duration_s=info["duration_s"], progress=progress, cancel=cancel)
        except FFmpegError as exc:
            log.warning("could not convert %s to H.264: %s", src.name, exc)
            temp.unlink(missing_ok=True)
            return src
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        if not temp.exists():
            return src
        if replace:
            final = src if src.suffix.lower() == ".mp4" else _unique(src.with_suffix(".mp4"))
            src.unlink(missing_ok=True)
        else:
            final = _unique(src.with_name(f"{src.stem}.h264.mp4"))
        _replace(temp, final)
        return final

    # -- WAV helpers -----------------------------------------------------
    def convert_wav(self, data: bytes, channels: int, sampwidth: int, rate: int) -> bytes:
        """Re-encode a WAV clip to the given channels / sample width / rate."""
        with tempfile.TemporaryDirectory(prefix="hoard-wav-") as tmp:
            src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
            src.write_bytes(data)
            self.run(["-i", src, "-ac", str(channels), "-ar", str(rate), "-c:a", _SAMPLE_CODEC.get(sampwidth, "pcm_s16le"), dst], timeout=120)
            return dst.read_bytes()

    def concat_wavs(self, clips: Sequence["tuple[bytes, float]"]) -> "tuple[bytes, float]":
        """Join ``(wav_bytes, pause_after_seconds)`` clips into one WAV: ``(wav_bytes, duration_s)``. The first clip sets the format; a
        clip in another format is converted with ffmpeg (only then is ffmpeg needed)."""
        if not clips:
            raise FFmpegError("No audio was produced.", code="failed")
        target: Optional[tuple[int, int, int]] = None
        frames: list[bytes] = []
        total = 0
        for data, pause in clips:
            ch, sw, rate, pcm = _wav_params(data)
            if target is None:
                target = (ch, sw, rate)
            elif (ch, sw, rate) != target:
                ch, sw, rate, pcm = _wav_params(self.convert_wav(data, *target))
                if (ch, sw, rate) != target:
                    raise FFmpegError("A clip could not be converted to the audio format of the first one.")
            frame_size = target[0] * target[1]
            pcm = pcm[: len(pcm) - (len(pcm) % frame_size)]
            frames.append(pcm)
            total += len(pcm) // frame_size
            if pause and pause > 0:
                n = int(round(target[2] * pause))
                frames.append((b"\x80" if target[1] == 1 else b"\x00") * (n * frame_size))
                total += n
        assert target is not None
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(target[0])
            w.setsampwidth(target[1])
            w.setframerate(target[2])
            w.writeframes(b"".join(frames))
        return out.getvalue(), total / float(target[2])


def _wav_params(data: bytes) -> "tuple[int, int, int, bytes]":
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    for i in range(2, 1000):
        candidate = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem} ({os.getpid()}){target.suffix}")


def _replace(src: Path, dst: Path) -> None:
    """``os.replace`` that survives Windows briefly locking the target."""
    import time

    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.4)
