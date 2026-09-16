"""Pistas, clips y marcadores sobre un documento ``timeline`` — WP13.

Read/shape helpers over the same plain-dict content
``src/creator/ops/timeline_ops.py`` (WP12) already mutates — this module
does not introduce a second representation, it is the validation/read layer
those ops (and the Studio projection in ``project_view.py``) share.

Per-track validation: clips on the SAME track must be strictly ordered and
non-overlapping (a track is one lane; two clips claiming the same tick have
no defined render order). Overlap ACROSS tracks is normal and never
checked here — a video track and its audio track occupy the same span on
purpose (arch doc, "Tiempo, regiones y operaciones").

Markers are a document-level, track-independent list
(``content.markers``, additive — not part of the WP02/WP12 required shape,
so an old document without it is still valid; every reader here treats a
missing key as an empty list). They give the "pistas ... marcadores" the
work-pack ficha asks for without inventing a new ``track.kind`` the WP02
document-shape validator does not know about (CONTRATO.md: no crear
autoridades paralelas — ``track.kind`` stays exactly the 5 values
``src/creator/documents.py`` validates).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation
from ..ops.model import Rational, TimeRange, find_by_id, index_by_id

TRACK_KINDS = ("video", "audio", "image", "caption", "overlay")

# Kinds that are meaningfully "subtitles" for UI grouping — caption tracks.
SUBTITLE_TRACK_KIND = "caption"


# ── tracks / clips (read side) ──────────────────────────────────────────

def get_tracks(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise InvalidOperation("timeline.content.tracks must be an array")
    return tracks


def get_track(content: Dict[str, Any], track_id: str) -> Dict[str, Any]:
    return find_by_id(get_tracks(content), track_id, what="track")


def clip_range(clip: Dict[str, Any]) -> TimeRange:
    return TimeRange.from_dict(
        clip, start_key="timeline_start_ticks", duration_key="timeline_duration_ticks",
        field_name=f"clip {clip.get('id')!r}",
    )


def clip_source_range(clip: Dict[str, Any]) -> TimeRange:
    return TimeRange.from_dict(clip.get("source_range"), field_name=f"clip {clip.get('id')!r}.source_range")


def clip_source_clock(clip: Dict[str, Any]) -> Rational:
    return Rational.from_dict(clip.get("source_clock"), field_name=f"clip {clip.get('id')!r}.source_clock")


def ordered_clips(track: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Clips on ``track`` sorted by their timeline start — the order a
    player/exporter renders them in, and the order ``validate_track`` walks
    to check for gaps/overlaps."""
    clips = track.get("clips")
    if not isinstance(clips, list):
        raise InvalidOperation(f"track {track.get('id')!r}.clips must be an array")
    return sorted(clips, key=lambda c: clip_range(c).start_ticks)


def validate_track(track: Dict[str, Any]) -> None:
    """Raises :class:`InvalidOperation` the first clip pair on this SAME
    track that overlaps. Does not check anything about other tracks."""
    clips = ordered_clips(track)
    for a, b in zip(clips, clips[1:]):
        ra, rb = clip_range(a), clip_range(b)
        if ra.overlaps(rb):
            raise InvalidOperation(
                f"clips {a.get('id')!r} and {b.get('id')!r} overlap on track {track.get('id')!r}"
            )


def validate_all_tracks(content: Dict[str, Any]) -> None:
    for track in get_tracks(content):
        validate_track(track)


def track_span(track: Dict[str, Any]) -> Optional[TimeRange]:
    """The ``[earliest start, latest end)`` span this track's clips occupy,
    or ``None`` for an empty track. Not the document ``duration_ticks``
    (which can be longer, e.g. trailing silence)."""
    clips = ordered_clips(track)
    if not clips:
        return None
    start = clip_range(clips[0]).start_ticks
    end = max(clip_range(c).end_ticks for c in clips)
    return TimeRange(start, end - start)


def cut_points(track: Dict[str, Any]) -> List[int]:
    """Every clip boundary (start and end tick) on ``track``, deduplicated
    and sorted — the "cortes" a snapping pass offers, per clip."""
    points = set()
    for c in track.get("clips") or []:
        r = clip_range(c)
        points.add(r.start_ticks)
        points.add(r.end_ticks)
    return sorted(points)


# ── markers (document-level, track-independent) ────────────────────────

def get_markers(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    markers = content.get("markers", [])
    if not isinstance(markers, list):
        raise InvalidOperation("timeline.content.markers must be an array")
    return markers


def marker_tick(marker: Dict[str, Any]) -> int:
    return TimeRange.from_dict(
        {"start_ticks": marker.get("at_ticks"), "duration_ticks": "1"},
        field_name=f"marker {marker.get('id')!r}",
    ).start_ticks


def validate_markers(content: Dict[str, Any]) -> None:
    markers = get_markers(content)
    seen_ids = set()
    for m in markers:
        if not isinstance(m, dict):
            raise InvalidOperation("each marker must be an object")
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            raise InvalidOperation("marker.id must be a non-empty string")
        if mid in seen_ids:
            raise InvalidOperation(f"duplicate marker id: {mid!r}")
        seen_ids.add(mid)
        marker_tick(m)  # structural check, raises on a bad at_ticks
        label = m.get("label")
        if label is not None and not isinstance(label, str):
            raise InvalidOperation("marker.label must be a string when present")


def add_marker(content: Dict[str, Any], marker_id: str, at_ticks: Any, label: Optional[str] = None) -> Dict[str, Any]:
    """Pure: returns a NEW content dict with the marker appended (used by
    ``timeline_ops.add_marker`` — this function does the validated shape
    work, the op wrapper does the ``require_kind``/copy-on-write plumbing
    every other op in this package follows)."""
    if not isinstance(marker_id, str) or not marker_id:
        raise InvalidOperation("marker id must be a non-empty string")
    markers = list(get_markers(content))
    if any(m.get("id") == marker_id for m in markers):
        raise InvalidOperation(f"marker id already exists: {marker_id!r}")
    tick = TimeRange.from_dict(
        {"start_ticks": at_ticks, "duration_ticks": "1"}, field_name="marker.at_ticks",
    ).start_ticks
    new_marker: Dict[str, Any] = {"id": marker_id, "at_ticks": str(tick)}
    if label is not None:
        if not isinstance(label, str):
            raise InvalidOperation("marker.label must be a string")
        new_marker["label"] = label
    markers.append(new_marker)
    new_content = copy.deepcopy(content)
    new_content["markers"] = markers
    return new_content


__all__ = [
    "TRACK_KINDS", "SUBTITLE_TRACK_KIND", "get_tracks", "get_track", "clip_range",
    "clip_source_range", "clip_source_clock", "ordered_clips", "validate_track",
    "validate_all_tracks", "track_span", "cut_points", "get_markers", "marker_tick",
    "validate_markers", "add_marker",
]
