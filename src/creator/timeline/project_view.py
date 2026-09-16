"""Compact projection — WP13.

Two read-only views over a ``timeline`` document's ``content``, both pure
functions of the same plain dict WP02/WP12 already store:

* :func:`project_view` — a flattened, pre-sorted structure the Studio's
  virtualized ``Timeline.tsx`` renders directly (one pass per track, clips
  already ordered, markers merged in with their ticks resolved) instead of
  re-deriving order/spans in the browser on every render.
* :func:`to_edl` — a minimal CMX3600-flavoured EDL (one video track's cut
  list) for a simple exporter/interchange path. Deliberately "simple": one
  video track, no dissolves/wipes (WP09's transitions are WP13-adjacent
  future work, not modelled here), ticks converted to the EDL's own
  timecode at the document's own clock.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation
from ..ops.model import Rational
from . import tracks as tracks_mod


def project_view(content: Dict[str, Any]) -> Dict[str, Any]:
    """A flat, UI-ready projection: does not mutate or re-validate deeply
    (callers wanting a guarantee call ``validate.validate`` first) — this
    is a SORT + FLATTEN, nothing more, so a virtualized list can window over
    it directly."""
    clock = Rational.from_dict(content.get("clock"), field_name="timeline.content.clock")
    duration = content.get("duration_ticks")

    out_tracks: List[Dict[str, Any]] = []
    for track in tracks_mod.get_tracks(content):
        clips = tracks_mod.ordered_clips(track)
        out_clips = []
        for clip in clips:
            r = tracks_mod.clip_range(clip)
            out_clips.append({
                "id": clip.get("id"),
                "asset_ref": clip.get("asset_ref"),
                "start_ticks": str(r.start_ticks),
                "end_ticks": str(r.end_ticks),
                "duration_ticks": str(r.duration_ticks),
                "gain_db": clip.get("gain_db"),
            })
        span = tracks_mod.track_span(track)
        out_tracks.append({
            "id": track.get("id"),
            "kind": track.get("kind"),
            "locked": bool(track.get("locked")),
            "clips": out_clips,
            "span": {"start_ticks": str(span.start_ticks), "end_ticks": str(span.end_ticks)} if span else None,
        })

    markers = []
    for m in tracks_mod.get_markers(content):
        tick = tracks_mod.marker_tick(m)
        markers.append({"id": m.get("id"), "at_ticks": str(tick), "label": m.get("label")})
    markers.sort(key=lambda m: int(m["at_ticks"]))

    return {
        "clock": clock.to_dict(),
        "duration_ticks": duration,
        "tracks": out_tracks,
        "markers": markers,
    }


def _ticks_to_timecode(ticks: int, clock: Rational) -> str:
    """``HH:MM:SS:FF`` at the document's own rate, integer frame arithmetic
    only (``clock`` IS the frame rate for a video-authoritative timeline —
    an audio-only document has no meaningful timecode and callers should
    not call this for one)."""
    fps_num, fps_den = clock.numerator, clock.denominator
    # frame index = ticks (clock ticks ARE frames when clock is the frame rate)
    frame = ticks
    fps_whole = -(-fps_num // fps_den)  # ceil, for the frames-per-second display digit count
    seconds_total, frame_in_sec = divmod(frame, fps_whole) if fps_whole > 0 else (0, frame)
    hours, rem = divmod(seconds_total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frame_in_sec:02d}"


def to_edl(content: Dict[str, Any], *, track_id: Optional[str] = None, title: str = "FAUSTUS_EDL") -> str:
    """Minimal CMX3600 EDL text for one video track (the first ``video``
    track, or ``track_id`` when given). Raises :class:`InvalidOperation`
    when there is no matching video track — an EDL with zero events is not
    a useful export."""
    clock = Rational.from_dict(content.get("clock"), field_name="timeline.content.clock")
    tracks = tracks_mod.get_tracks(content)
    if track_id is not None:
        track = tracks_mod.get_track(content, track_id)
    else:
        video_tracks = [t for t in tracks if t.get("kind") == "video"]
        if not video_tracks:
            raise InvalidOperation("to_edl requires at least one 'video' track")
        track = video_tracks[0]

    clips = tracks_mod.ordered_clips(track)
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME"]
    for i, clip in enumerate(clips, start=1):
        r = tracks_mod.clip_range(clip)
        src = tracks_mod.clip_source_range(clip)
        src_clock = tracks_mod.clip_source_clock(clip)
        rec_in = _ticks_to_timecode(r.start_ticks, clock)
        rec_out = _ticks_to_timecode(r.end_ticks, clock)
        src_in = _ticks_to_timecode(src.start_ticks, src_clock)
        src_out = _ticks_to_timecode(src.end_ticks, src_clock)
        reel = str(clip.get("asset_ref") or "AX")[:8].upper() or "AX"
        lines.append(f"{i:03d}  {reel:<8} V     C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {clip.get('id')}")
    return "\n".join(lines) + "\n"


__all__ = ["project_view", "to_edl"]
