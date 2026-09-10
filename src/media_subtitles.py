"""
media_subtitles.py — MEDIA-05: subtitle export (SRT/VTT), pure and offline.

`services/local_video.transcribe()` already produces timestamped segments
(`{"start", "end", "text"}`, validated by `services.local_video.validate_segments`
— ordered, non-overlapping, inside the clip) for the existing transcribe/dub
tools; nothing in the repo turns that into the two subtitle formats every
video editor and platform actually wants. This module is exactly that
conversion, and nothing else: pure functions over already-validated
segments, no ffmpeg, no file I/O, so the video and audio they describe are
never at risk of being touched by exporting a caption file.

Deliberately reuses `services.local_video.validate_segments` (imported, not
reimplemented) wherever a caller needs it — the ordering/overlap/duration
rules for a segment list are that module's authority, not a second copy of
them here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence


def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _vtt_timestamp(seconds: float) -> str:
    return _srt_timestamp(seconds).replace(",", ".")


def _clean_text(text: str) -> str:
    # A caption line is shown, not parsed; a blank line inside one would
    # split it into two cues in both formats, silently changing the timing
    # they are shown at.
    return " ".join(str(text or "").split())


def to_srt(segments: Sequence[Mapping[str, Any]]) -> str:
    """Segments (`start`, `end`, `text`, seconds) → a `.srt` file's text.

    Cues are numbered in the order given — callers that already ran
    `services.local_video.validate_segments` have them ordered and
    non-overlapping, but this function does not re-check that itself: it
    formats what it is given, the same separation `media_workflows.render`
    keeps between "is this well-formed" and "turn it into the output
    shape"."""
    blocks = []
    for i, seg in enumerate(segments, start=1):
        text = _clean_text(seg.get("text", ""))
        if not text:
            continue
        blocks.append(f"{i}\n{_srt_timestamp(float(seg['start']))} --> "
                      f"{_srt_timestamp(float(seg['end']))}\n{text}")
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def to_vtt(segments: Sequence[Mapping[str, Any]]) -> str:
    """Segments → a `.vtt` file's text (the same cues as `to_srt`, WebVTT's
    header and `.` decimal separator)."""
    blocks = []
    for seg in segments:
        text = _clean_text(seg.get("text", ""))
        if not text:
            continue
        blocks.append(f"{_vtt_timestamp(float(seg['start']))} --> "
                      f"{_vtt_timestamp(float(seg['end']))}\n{text}")
    body = "\n\n".join(blocks)
    return "WEBVTT\n\n" + body + ("\n" if body else "")


def export(segments: Sequence[Mapping[str, Any]], fmt: str) -> Dict[str, str]:
    """`{"text", "filename", "media_type"}` for the requested format — the
    shape an HTTP route hands back without knowing SRT/VTT syntax itself."""
    if fmt == "srt":
        return {"text": to_srt(segments), "filename": "subtitles.srt",
               "media_type": "application/x-subrip"}
    if fmt == "vtt":
        return {"text": to_vtt(segments), "filename": "subtitles.vtt",
               "media_type": "text/vtt"}
    raise ValueError(f"unsupported subtitle format {fmt!r}; use 'srt' or 'vtt'")
