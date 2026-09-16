"""tests/creator_harness/fixtures.py — WP36: deterministic audiovisual fixtures.

`docs/spec/creator/plan/docs/08_PRUEBAS_Y_ACEPTACION.md`, "Fixtures
audiovisuales": small, generated, controlled — never a personal recording or
a cloned voice kept around "for convenience". Every generator here is a pure
function of its arguments: the same call produces byte-identical output
every time, so a failing test names an exact reproducible input.

Three media kinds, three different honesty problems:

* **Images** (`png_bytes`/`jpeg_bytes`) — four flat-colour quadrants at
  known coordinates, so a compositor test can assert on a specific pixel
  rather than "the file opened". `corrupt_png_bytes()`/`truncated_bytes()`
  make inputs a real adapter's `collect()` MUST reject, never register.
* **Audio** (`wav_bytes`) — a pure sine tone (or silence) written with the
  stdlib `wave` module and `math.sin`, no audio library dependency. Two
  tones at once (`overlapping_voices_wav_bytes`) stands in for "audio de
  voces solapadas autorizado" without shipping an actual recording.
* **Video** (`make_mp4`) — the only fixture that needs a real subprocess
  (`ffmpeg`'s `testsrc`/`sine` lavfi sources). `ffmpeg_available()` is
  cached once per process (`functools.lru_cache`, not per call to
  `shutil.which`) and every caller that cannot make one skips explicitly
  with `pytest.skip(...)` rather than silently passing.
"""
from __future__ import annotations

import functools
import io
import math
import os
import shutil
import struct
import subprocess
import wave
from typing import Optional, Sequence, Tuple

__all__ = [
    "ffmpeg_available", "ffprobe_available",
    "png_bytes", "jpeg_bytes", "corrupt_png_bytes", "truncated_bytes",
    "wav_bytes", "silence_wav_bytes", "overlapping_voices_wav_bytes",
    "make_mp4", "make_vfr_mp4", "write_bytes",
]


# ── binary availability (cached once per process) ─────────────────────────

@functools.lru_cache(maxsize=1)
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


@functools.lru_cache(maxsize=1)
def ffprobe_available() -> bool:
    return bool(shutil.which("ffprobe"))


def write_bytes(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ── images: PIL is a hard dependency of this repo's image pipeline
# (src/media_edit_projects.py already imports it unconditionally), so no
# optional-import fallback is needed here. ─────────────────────────────────

def png_bytes(width: int = 16, height: int = 16,
               quadrant_colors: Optional[Sequence[Tuple[int, int, int, int]]] = None) -> bytes:
    """RGBA PNG with four flat quadrants (top-left, top-right, bottom-left,
    bottom-right) at KNOWN colours, so a test can assert
    `pixel(w//4, h//4) == quadrant_colors[0]` instead of trusting `mode`
    alone."""
    from PIL import Image

    colors = quadrant_colors or ((255, 0, 0, 255), (0, 255, 0, 255),
                                  (0, 0, 255, 128), (255, 255, 0, 64))
    img = Image.new("RGBA", (width, height))
    half_w, half_h = width // 2, height // 2
    for y in range(height):
        for x in range(width):
            quadrant = (0 if x < half_w else 1) + (0 if y < half_h else 2)
            img.putpixel((x, y), colors[quadrant])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(width: int = 16, height: int = 16) -> bytes:
    """A real (lossy) JPEG — deliberately a DIFFERENT format from
    `png_bytes()` so an adapter/inspection path that only handles one
    container is caught rather than passing on a coincidence."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(20, 120, 220))
    for x in range(0, width, 2):
        for y in range(0, height, 2):
            img.putpixel((x, y), (220, 20, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def corrupt_png_bytes(width: int = 16, height: int = 16) -> bytes:
    """A real PNG whose IDAT stream is overwritten with garbage: the
    signature and header are intact (a naive "does it start with the PNG
    magic bytes" check would pass this), but nothing can actually decode
    the pixels. This is the shape a `collect()` MUST reject via real
    inspection (`valid=False`), not via a byte-count heuristic."""
    good = png_bytes(width, height)
    marker = good.find(b"IDAT")
    if marker < 0:
        return good[: max(1, len(good) // 2)]
    # Corrupt every byte of the IDAT chunk's payload (leave the length/type/
    # CRC framing so the chunk still looks structurally present).
    payload_start = marker + 4
    payload_end = len(good) - 8  # leave room for IEND
    corrupted = bytearray(good)
    for i in range(payload_start, min(payload_end, len(corrupted))):
        corrupted[i] = (corrupted[i] ^ 0xFF) & 0xFF
    return bytes(corrupted)


def truncated_bytes(data: bytes, keep_fraction: float = 0.4) -> bytes:
    """The first `keep_fraction` of `data` — a download cut off mid-transfer,
    the shape `disk lleno`/`descarga interrumpida` fault injection needs."""
    keep_fraction = max(0.0, min(1.0, keep_fraction))
    return data[: int(len(data) * keep_fraction)]


# ── audio: stdlib `wave` + `math.sin`, no audio library dependency ────────

def _pcm16_sine(seconds: float, freq_hz: float, sample_rate: int,
                 amplitude: float = 0.6) -> bytes:
    n = max(1, int(seconds * sample_rate))
    amp = max(0.0, min(1.0, amplitude)) * 32767
    samples = (int(amp * math.sin(2 * math.pi * freq_hz * (i / sample_rate)))
               for i in range(n))
    return struct.pack(f"<{n}h", *samples)


def wav_bytes(seconds: float = 0.5, freq_hz: float = 440.0,
               sample_rate: int = 8000) -> bytes:
    """Mono 16-bit PCM WAV, a pure sine tone at `freq_hz` — the WAV's own
    duration (`nframes / framerate`) is therefore a fully known, checkable
    quantity, unlike a real recording."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(_pcm16_sine(seconds, freq_hz, sample_rate))
    return buf.getvalue()


def silence_wav_bytes(seconds: float = 0.2, sample_rate: int = 8000) -> bytes:
    n = max(1, int(seconds * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


def overlapping_voices_wav_bytes(seconds: float = 1.0, sample_rate: int = 8000,
                                   freq_a: float = 220.0, freq_b: float = 330.0) -> bytes:
    """Two synthetic tones SUMMED into one mono track, standing in for
    "audio de voces solapadas autorizado" (08_PRUEBAS_Y_ACEPTACION.md)
    without distributing an actual recorded voice. Deterministic and
    licence-free — every byte is `sin(a) + sin(b)`."""
    n = max(1, int(seconds * sample_rate))
    amp = 0.4 * 32767

    def clamp16(v: float) -> int:
        return max(-32768, min(32767, int(v)))

    samples = []
    for i in range(n):
        t = i / sample_rate
        v = amp * math.sin(2 * math.pi * freq_a * t) + amp * math.sin(2 * math.pi * freq_b * t)
        samples.append(clamp16(v))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


# ── video: the one fixture that needs a real subprocess ───────────────────

def make_mp4(path: str, *, seconds: float = 1.0, size: str = "64x64",
              fps: int = 5, with_audio: bool = True, timeout: int = 30) -> str:
    """A short H.264 MP4 with a frame-counter test pattern, built by
    ffmpeg's own `testsrc` lavfi source (a counter baked into the frames
    themselves is what `08_PRUEBAS_Y_ACEPTACION.md`'s "vídeo con contador de
    frame" fixture asks for — ffmpeg draws it, this function never has to).
    Raises `RuntimeError` naming ffmpeg's stderr on failure; callers decide
    whether that means `pytest.skip` or a real test failure."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not on PATH")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    argv = [
        "ffmpeg", "-y", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={fps}",
    ]
    if with_audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                  "-c:a", "aac", "-shortest"]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p", path]
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(
            f"ffmpeg failed to build fixture mp4 (exit {proc.returncode}): "
            f"{(proc.stderr or b'').decode('utf-8', 'replace')[-2000:]}")
    return path


def make_vfr_mp4(path: str, *, seconds: float = 1.0, size: str = "64x64",
                   timeout: int = 30) -> str:
    """A variable-frame-rate clip: `testsrc2` at 30fps re-timed through a
    filter that drops frames unevenly, so a timeline test can exercise a
    real "sample VFR" input rather than a synthetic assumption of constant
    frame spacing (08_PRUEBAS_Y_ACEPTACION.md, "sample VFR")."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not on PATH")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    argv = [
        "ffmpeg", "-y", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate=30",
        "-vf", "select='not(mod(n,3))+not(mod(n,7))',setpts=N/(30*TB)",
        "-vsync", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p", path,
    ]
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(
            f"ffmpeg failed to build VFR fixture mp4 (exit {proc.returncode}): "
            f"{(proc.stderr or b'').decode('utf-8', 'replace')[-2000:]}")
    return path
