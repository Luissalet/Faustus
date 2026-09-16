"""Snapping — WP13. Pure functions: given a candidate tick and a document's
tracks/markers, find the nearest "interesting" tick within a threshold —
clip edges (cuts), markers, or the frame grid. No I/O, no mutation.

The Studio's ``Timeline.tsx`` drag/keyboard handling calls the mirror of
this in ``studio/src/adapters/creator_timeline.ts`` client-side for instant
feedback, then sends the FINAL snapped tick through a normal typed op
(``timeline.move``/``timeline.trim``/``timeline.snap``); this module is the
one place both server-side validation and the client's own logic can be
checked against, since a client snap suggestion is advisory only — the
server never trusts a client-computed value without revalidating it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation
from . import tracks as tracks_mod

SnapSource = str  # "cut" | "marker" | "frame"


@dataclass(frozen=True)
class SnapCandidate:
    ticks: int
    source: SnapSource
    label: Optional[str] = None


@dataclass(frozen=True)
class SnapResult:
    input_ticks: int
    snapped_ticks: int
    candidate: Optional[SnapCandidate]  # None when nothing was within threshold


def _frame_grid_candidates(near: int, frame_ticks: int, window: int) -> List[SnapCandidate]:
    if frame_ticks <= 0:
        raise InvalidOperation("frame_ticks must be > 0")
    lo = max(0, near - window)
    hi = near + window
    first = (lo // frame_ticks) * frame_ticks
    out = []
    t = first
    while t <= hi:
        if t >= 0:
            out.append(SnapCandidate(t, "frame"))
        t += frame_ticks
    return out


def snap_candidates(
    content: Dict[str, Any], near_ticks: int, *, window_ticks: int,
    include_tracks: bool = True, include_markers: bool = True,
    frame_ticks: Optional[int] = None, exclude_track_id: Optional[str] = None,
) -> List[SnapCandidate]:
    """Every snap target within ``window_ticks`` of ``near_ticks``. Cuts and
    markers ALWAYS take priority over the frame grid in :func:`snap` (a
    marker or another clip's edge is a more meaningful anchor than an
    arbitrary frame boundary), enforced by :func:`snap`'s ordering, not by
    this collector — this function returns every candidate unranked."""
    if isinstance(near_ticks, bool) or not isinstance(near_ticks, int) or near_ticks < 0:
        raise InvalidOperation("near_ticks must be a non-negative integer")
    if not isinstance(window_ticks, int) or window_ticks < 0:
        raise InvalidOperation("window_ticks must be a non-negative integer")

    out: List[SnapCandidate] = []
    lo, hi = near_ticks - window_ticks, near_ticks + window_ticks

    if include_tracks:
        for track in tracks_mod.get_tracks(content):
            if track.get("id") == exclude_track_id:
                continue
            for point in tracks_mod.cut_points(track):
                if lo <= point <= hi:
                    out.append(SnapCandidate(point, "cut", track.get("id")))

    if include_markers:
        for marker in tracks_mod.get_markers(content):
            tick = tracks_mod.marker_tick(marker)
            if lo <= tick <= hi:
                out.append(SnapCandidate(tick, "marker", marker.get("label") or marker.get("id")))

    if frame_ticks is not None:
        out.extend(_frame_grid_candidates(near_ticks, frame_ticks, window_ticks))

    return out


_PRIORITY = {"cut": 0, "marker": 0, "frame": 1}


def snap(
    content: Dict[str, Any], near_ticks: int, *, window_ticks: int,
    include_tracks: bool = True, include_markers: bool = True,
    frame_ticks: Optional[int] = None, exclude_track_id: Optional[str] = None,
) -> SnapResult:
    """Picks the single best candidate: cut/marker candidates rank above
    frame-grid candidates regardless of distance (a 1-tick-further marker
    beats a closer frame line), then nearest distance, then lowest tick as a
    deterministic tiebreak. Returns the input unchanged (``candidate=None``)
    when nothing is within the window."""
    candidates = snap_candidates(
        content, near_ticks, window_ticks=window_ticks, include_tracks=include_tracks,
        include_markers=include_markers, frame_ticks=frame_ticks, exclude_track_id=exclude_track_id,
    )
    if not candidates:
        return SnapResult(near_ticks, near_ticks, None)
    best = min(candidates, key=lambda c: (_PRIORITY[c.source], abs(c.ticks - near_ticks), c.ticks))
    return SnapResult(near_ticks, best.ticks, best)


__all__ = ["SnapCandidate", "SnapResult", "snap_candidates", "snap"]
