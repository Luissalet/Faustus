"""Practical offline video localization. No provider clients or model downloads.

Whisper uses existing local weights only; dubbing uses installed Windows voices.
All FFmpeg inputs are local files with fixed demuxers and a file-only protocol.
"""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import wave

MAX_DURATION = 180
MAX_SEGMENTS = 80
DEMUXERS = {".mp4": "mov", ".mov": "mov", ".webm": "matroska", ".mkv": "matroska"}


def command(args: list[str], timeout: int = 90) -> bytes:
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError("Local media processing failed. Check the video format and installed FFmpeg.")
    return result.stdout


def input_args(path: Path) -> list[str]:
    demuxer = DEMUXERS.get(path.suffix.lower())
    if not demuxer:
        raise ValueError("Use an MP4, MOV, WebM or MKV video")
    return ["-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(path)]


def video_duration(path: Path) -> float:
    probe = shutil.which("ffprobe")
    if not probe:
        raise ValueError("Install FFmpeg and FFprobe on the server PATH first")
    data = json.loads(command([probe, "-v", "error", *input_args(path), "-show_entries",
                               "format=duration:stream=codec_type,width,height", "-of", "json"], timeout=20))
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
        raise ValueError("Local localization accepts videos up to 3 minutes")
    if not any(row.get("codec_type") == "video" for row in data.get("streams", [])):
        raise ValueError("The file contains no video stream")
    if any(int(row.get("width", 0))*int(row.get("height", 0)) > 1920*1080 for row in data.get("streams", [])):
        raise ValueError("Local localization accepts video up to 1080p")
    return duration


def validate_segments(rows: list, duration: float) -> list[dict]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SEGMENTS:
        raise ValueError("Use 1–80 subtitle segments")
    result, previous, chars = [], 0.0, 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"start", "end", "text"}:
            raise ValueError("Each segment needs start, end and text")
        start, end, text = row["start"], row["end"], row["text"]
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (start, end)):
            raise ValueError("Segment times must be finite numbers")
        if start < previous or end-start < 0.1 or end > duration + 0.05:
            raise ValueError("Segments must be ordered, non-overlapping and inside the video")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000 or "\x00" in text:
            raise ValueError("Each segment needs 1–1000 text characters")
        chars += len(text)
        if chars > 10000:
            raise ValueError("Use at most 10,000 text characters")
        result.append({"start": float(start), "end": float(end), "text": text.strip()})
        previous = end
    return result


def transcribe(path: Path, language: str, model_name: str) -> dict:
    duration = video_duration(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("Install FFmpeg on the server PATH first")
    audio = path.parent / "speech.wav"
    command([ffmpeg, "-v", "error", "-nostdin", *input_args(path), "-vn", "-t", str(MAX_DURATION),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
    try:
        from faster_whisper import WhisperModel
        # Explicit offline flag prevents an absent model from starting a download.
        model = WhisperModel(model_name, device="cpu", compute_type="int8", local_files_only=True)
    except Exception as exc:
        raise ValueError("Local Whisper weights are unavailable. Configure an already downloaded STT model, or enter the transcript manually.") from exc
    segments, info = model.transcribe(str(audio), language=None if language == "auto" else language,
                                      vad_filter=True, beam_size=1)
    rows = [{"start": max(0, s.start), "end": min(duration, s.end), "text": s.text.strip()}
            for s in segments if s.text.strip() and s.start < duration]
    return {"duration": duration, "language": info.language, "segments": validate_segments(rows, duration)}


def tempo_filter(ratio: float) -> str:
    if not math.isfinite(ratio) or not 0.25 <= ratio <= 4:
        raise ValueError("Narration does not fit this segment. Shorten the text or adjust segment times (allowed speed: 0.25–4x)")
    factors = []
    while ratio > 2:
        factors.append(2.0); ratio /= 2
    while ratio < 0.5:
        factors.append(0.5); ratio /= 0.5
    return ",".join(f"atempo={value:.8f}" for value in [*factors, ratio])


def dub(path: Path, rows: list, language: str) -> Path:
    duration = video_duration(path)
    rows = validate_segments(rows, duration)
    if os.name != "nt":
        raise ValueError("Offline dubbing currently uses Windows installed voices; subtitles work on every platform")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("Install FFmpeg on the server PATH first")
    from services.tts.system_voice import synthesize_system
    # A three-minute mono PCM timeline is bounded to under 6 MB.
    timeline = bytearray(math.ceil(duration * 16000) * 2)
    for index, row in enumerate(rows):
        audio = synthesize_system(row["text"], language=language)
        source = path.parent / f"voice-{index}.wav"
        source.write_bytes(audio)
        with wave.open(io.BytesIO(audio)) as wav:
            voice_duration = wav.getnframes() / wav.getframerate()
        if voice_duration <= 0:
            raise ValueError("The installed voice returned empty audio")
        span = row["end"] - row["start"]
        target = path.parent / f"fit-{index}.wav"
        command([ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(source),
                 "-af", tempo_filter(voice_duration/span)+",apad", "-t", str(span), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)])
        with wave.open(str(target)) as wav:
            pcm = wav.readframes(wav.getnframes())
        offset = round(row["start"]*16000)*2
        length = min(len(pcm), round(span*16000)*2, len(timeline)-offset)
        timeline[offset:offset+length] = pcm[:length]
    narration = path.parent / "narration.wav"
    with wave.open(str(narration), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(timeline)
    output = path.parent / "localized.mp4"
    command([ffmpeg, "-v", "error", "-nostdin", *input_args(path), "-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(narration),
             "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-t", str(duration), "-movflags", "+faststart", str(output)], timeout=180)
    return output


if __name__ == "__main__":
    import sys
    try:
        payload = json.loads(sys.stdin.read(150000))
        path = Path(payload["path"])
        if payload["mode"] == "transcribe":
            result = transcribe(path, payload["language"], payload["model"])
        else:
            dub(path, payload["segments"], payload["language"])
            result = {"ok": True}
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, ValueError) else "Offline processing failed. Check installed tools and model weights."}))
        sys.exit(1)
