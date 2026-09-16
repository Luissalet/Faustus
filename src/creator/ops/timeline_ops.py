"""Timeline (video/audio cut) operations — WP12 / AST07.

Overlap is validated PER TRACK, never across tracks (arch doc, "Tiempo,
regiones y operaciones": a multi-track timeline is expected to have a video
track and an audio track occupying the same span at once — that is the
normal case, not a conflict). Two clips on the SAME track occupying
overlapping timeline ticks is rejected: a track is a single lane, and two
clips claiming the same frame there has no defined render order.

All ticks referenced here are the document's own ``clock`` (``timeline_*``
fields); a clip's ``source_range``/``source_clock`` describe a DIFFERENT
tick rate (the source media's), never assumed equal to the document clock
— ``trim``/``split`` only ever touch a clip's own two clocks together,
never mix one clip's source ticks with another clip's timeline position.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation
from .model import Rational, RetimingMap, TimeRange, find_by_id, index_by_id, require_kind

TRACK_KINDS = ("video", "audio", "image", "caption", "overlay")


def _tracks(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise InvalidOperation("timeline.content.tracks must be an array")
    return tracks


def _track(content: Dict[str, Any], track_id: Any) -> Dict[str, Any]:
    return find_by_id(_tracks(content), track_id, what="track")


def _clip_range(clip: Dict[str, Any]) -> TimeRange:
    return TimeRange.from_dict(
        clip, start_key="timeline_start_ticks", duration_key="timeline_duration_ticks",
        field_name=f"clip {clip.get('id')!r}",
    )


def _require_not_locked(track: Dict[str, Any]) -> None:
    if track.get("locked"):
        raise InvalidOperation(f"track {track.get('id')!r} is locked")


def _check_no_overlap(clips: List[Dict[str, Any]], *, ignore_id: Optional[str] = None) -> None:
    ranges = []
    for c in clips:
        if c.get("id") == ignore_id:
            continue
        ranges.append((c.get("id"), _clip_range(c)))
    ranges.sort(key=lambda pair: pair[1].start_ticks)
    for (id_a, range_a), (id_b, range_b) in zip(ranges, ranges[1:]):
        if range_a.overlaps(range_b):
            raise InvalidOperation(f"clips {id_a!r} and {id_b!r} overlap on the same track")


def insert_clip(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)

    clip = op.get("clip")
    if not isinstance(clip, dict):
        raise InvalidOperation("timeline.insert_clip requires an object 'clip'")
    required = ("id", "asset_ref", "timeline_start_ticks", "source_range",
                "source_clock", "timeline_duration_ticks")
    for key in required:
        if key not in clip:
            raise InvalidOperation(f"timeline.insert_clip clip.{key} is required")
    if not isinstance(clip["id"], str) or not clip["id"]:
        raise InvalidOperation("timeline.insert_clip clip.id must be a non-empty string")
    if any(c.get("id") == clip["id"] for c in track["clips"]):
        raise InvalidOperation(f"clip id already exists on this track: {clip['id']!r}")

    # Validate the two clocks/ranges structurally (raises InvalidOperation on
    # a malformed shape) before it is allowed onto the track.
    _clip_range(clip)
    TimeRange.from_dict(clip["source_range"], field_name=f"clip {clip['id']!r}.source_range")
    Rational.from_dict(clip["source_clock"], field_name=f"clip {clip['id']!r}.source_clock")

    new_clip = copy.deepcopy(clip)
    candidate = list(track["clips"]) + [new_clip]
    _check_no_overlap(candidate)
    track["clips"] = candidate
    return content


def trim(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Adjusts a clip's ``source_range`` and, correspondingly, its
    ``timeline_duration_ticks`` (and optionally ``timeline_start_ticks`` for
    a trim from the head). Re-validates overlap on the track afterwards —
    a trim can never silently create one."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)
    clip = find_by_id(track["clips"], op.get("clip_id"), what="clip")

    new_source_range = TimeRange.from_dict(op.get("source_range"), field_name="timeline.trim.source_range")
    new_timeline_duration = op.get("timeline_duration_ticks", new_source_range.to_dict()["duration_ticks"])
    clip["source_range"] = new_source_range.to_dict()
    clip["timeline_duration_ticks"] = str(int(new_timeline_duration))
    if "timeline_start_ticks" in op:
        clip["timeline_start_ticks"] = str(int(TimeRange.from_dict(
            {"start_ticks": op["timeline_start_ticks"], "duration_ticks": clip["timeline_duration_ticks"]},
            field_name="timeline.trim",
        ).start_ticks))
    _clip_range(clip)  # structural re-check of the fields just written
    _check_no_overlap(track["clips"])
    return content


def split(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Splits one clip into two at an absolute document-clock tick
    (``at_ticks``), strictly inside the clip's span. The source range is
    divided by the SAME fraction as the timeline span (linear mapping —
    this op does not consult a retiming map), so ``source_duration`` on
    each half sums back to the original."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)
    idx = index_by_id(track["clips"], op.get("clip_id"), what="clip")
    clip = track["clips"][idx]
    clip_range = _clip_range(clip)

    at_ticks = op.get("at_ticks")
    if not isinstance(at_ticks, (int, str)) or isinstance(at_ticks, bool):
        raise InvalidOperation("timeline.split requires an integer/string 'at_ticks'")
    at_ticks = int(at_ticks)
    if not (clip_range.start_ticks < at_ticks < clip_range.end_ticks):
        raise InvalidOperation("timeline.split 'at_ticks' must be strictly inside the clip")

    new_clip_id = op.get("new_clip_id")
    if not isinstance(new_clip_id, str) or not new_clip_id:
        raise InvalidOperation("timeline.split requires a non-empty string 'new_clip_id'")
    if any(c.get("id") == new_clip_id for c in track["clips"]):
        raise InvalidOperation(f"clip id already exists on this track: {new_clip_id!r}")

    offset = at_ticks - clip_range.start_ticks
    fraction_num = offset
    fraction_den = clip_range.duration_ticks

    src_range = TimeRange.from_dict(clip["source_range"], field_name="clip.source_range")
    split_source_dur = (src_range.duration_ticks * fraction_num) // fraction_den
    split_source_dur = max(1, min(split_source_dur, src_range.duration_ticks - 1))

    first = copy.deepcopy(clip)
    first["timeline_duration_ticks"] = str(offset)
    first["source_range"] = {
        "start_ticks": str(src_range.start_ticks),
        "duration_ticks": str(split_source_dur),
    }

    second = copy.deepcopy(clip)
    second["id"] = new_clip_id
    second["timeline_start_ticks"] = str(at_ticks)
    second["timeline_duration_ticks"] = str(clip_range.duration_ticks - offset)
    second["source_range"] = {
        "start_ticks": str(src_range.start_ticks + split_source_dur),
        "duration_ticks": str(src_range.duration_ticks - split_source_dur),
    }

    track["clips"][idx] = first
    track["clips"].insert(idx + 1, second)
    _check_no_overlap(track["clips"])
    return content


def move(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Moves a clip to an absolute ``timeline_start_ticks``, on its current
    track or an explicit ``target_track_id``. Cross-track move requires the
    destination track to have the SAME ``kind`` as the source — moving an
    audio clip onto a caption track has no defined meaning."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    src_track = _track(content, op.get("track_id"))
    _require_not_locked(src_track)
    clip = find_by_id(src_track["clips"], op.get("clip_id"), what="clip")

    new_start = op.get("timeline_start_ticks")
    if new_start is None:
        raise InvalidOperation("timeline.move requires 'timeline_start_ticks'")
    clip = copy.deepcopy(clip)
    clip["timeline_start_ticks"] = str(int(new_start))
    _clip_range(clip)

    target_track_id = op.get("target_track_id", op.get("track_id"))
    dst_track = _track(content, target_track_id)
    if dst_track["id"] != src_track["id"] and dst_track.get("kind") != src_track.get("kind"):
        raise InvalidOperation(
            f"cannot move a {src_track.get('kind')!r} clip onto a {dst_track.get('kind')!r} track"
        )
    _require_not_locked(dst_track)

    src_track["clips"] = [c for c in src_track["clips"] if c["id"] != clip["id"]]
    if dst_track["id"] == src_track["id"]:
        dst_track["clips"].append(clip)
    else:
        dst_track["clips"].append(clip)
    _check_no_overlap(dst_track["clips"])
    return content


def ripple_delete(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Removes a clip and shifts every LATER clip on the SAME track left by
    the removed clip's timeline duration. Other tracks are untouched — a
    ripple across tracks would require them to already be in sync, which
    this op does not assume; a caller wanting that ripples each track with
    its own command."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)
    idx = index_by_id(track["clips"], op.get("clip_id"), what="clip")
    removed = track["clips"][idx]
    removed_range = _clip_range(removed)

    remaining = track["clips"][:idx] + track["clips"][idx + 1:]
    for c in remaining:
        c_range = _clip_range(c)
        if c_range.start_ticks >= removed_range.end_ticks:
            c["timeline_start_ticks"] = str(c_range.start_ticks - removed_range.duration_ticks)
    track["clips"] = remaining
    return content


def set_gain(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets a clip's ``gain_db`` (additive field, only meaningful for a
    track that can carry audio)."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    if track.get("kind") not in ("audio", "video"):
        raise InvalidOperation("timeline.set_gain only applies to 'audio' or 'video' tracks")
    clip = find_by_id(track["clips"], op.get("clip_id"), what="clip")
    gain_db = op.get("gain_db")
    if not isinstance(gain_db, (int, float)) or isinstance(gain_db, bool):
        raise InvalidOperation("timeline.set_gain requires a numeric 'gain_db'")
    clip["gain_db"] = float(gain_db)
    return content


def snap(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """WP13 additive op: moves a clip to the nearest snap target (another
    clip's cut, a marker, or the frame grid) to ``op['near_ticks']``, within
    ``op['window_ticks']``. Delegates the search to
    ``src/creator/timeline/snapping.py`` — a client-side snap preview is
    ALWAYS advisory; this is where the snapped position is actually decided
    and validated (overlap re-checked exactly like ``move``). No-ops (moves
    to ``near_ticks`` itself) when nothing is within the window, so a
    caller can always send this op instead of branching client-side on
    whether a candidate existed."""
    require_kind(doc, "timeline")
    from ..timeline import snapping  # local import: avoids a package-init cycle at module load

    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)
    clip = find_by_id(track["clips"], op.get("clip_id"), what="clip")

    near_ticks = op.get("near_ticks")
    if not isinstance(near_ticks, (int, str)) or isinstance(near_ticks, bool):
        raise InvalidOperation("timeline.snap requires an integer/string 'near_ticks'")
    near_ticks = int(near_ticks)
    window_ticks = op.get("window_ticks")
    if not isinstance(window_ticks, (int, str)) or isinstance(window_ticks, bool):
        raise InvalidOperation("timeline.snap requires an integer/string 'window_ticks'")
    window_ticks = int(window_ticks)
    frame_ticks = op.get("frame_ticks")
    if frame_ticks is not None:
        frame_ticks = int(frame_ticks)

    result = snapping.snap(
        content, near_ticks, window_ticks=window_ticks, frame_ticks=frame_ticks,
        include_tracks=bool(op.get("include_tracks", True)),
        include_markers=bool(op.get("include_markers", True)),
    )

    clip = copy.deepcopy(clip)
    clip["timeline_start_ticks"] = str(result.snapped_ticks)
    _clip_range(clip)
    track["clips"] = [clip if c["id"] == clip["id"] else c for c in track["clips"]]
    _check_no_overlap(track["clips"])
    return content


def retime(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """WP13 additive op: attaches (or replaces) a clip's ``retiming_map`` —
    a ``src/creator/timeline/retiming.py``-shaped list of source↔dest
    segments used by a downstream renderer/exporter to resolve a speed
    ramp or a cut-with-gap, instead of the clip's plain linear
    ``source_range`` mapping. Additive field: a clip with no
    ``retiming_map`` still means "linear, whole source_range", exactly as
    before this op existed. Structurally validated via
    ``ops.model.RetimingMap`` (rejects overlapping destination segments);
    does NOT check the map's ``dest`` span matches the clip's own timeline
    span — a caller building a partial/preview map is allowed to, and
    ``timeline.validate`` (WP13's read-side report) is where that kind of
    cross-check belongs, not a write-time hard failure."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)
    clip = find_by_id(track["clips"], op.get("clip_id"), what="clip")

    segments = op.get("retiming_map")
    if not isinstance(segments, list):
        raise InvalidOperation("timeline.retime requires an array 'retiming_map'")
    validated = RetimingMap.from_list(segments)  # raises InvalidOperation on a malformed/overlapping map

    clip["retiming_map"] = validated.to_list()
    return content


def add_marker(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """WP13 additive op: appends a document-level marker (see
    ``src/creator/timeline/tracks.py::add_marker`` for the shape/validation
    — track-independent, not a ``track.kind``)."""
    require_kind(doc, "timeline")
    from ..timeline import tracks as timeline_tracks

    marker_id = op.get("marker_id")
    at_ticks = op.get("at_ticks")
    label = op.get("label")
    return timeline_tracks.add_marker(doc["content"], marker_id, at_ticks, label)


def ripple_insert(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """WP13 additive op: inserts a new clip at ``op['clip']['timeline_start_ticks']``
    on ``track_id`` and shifts every clip on that SAME track whose current
    start is at or after the insertion point to the right by the new
    clip's ``timeline_duration_ticks`` — the ripple counterpart to
    ``ripple_delete`` (WP12), so an editor can open a gap for an insert
    without hand-computing every later clip's new start. Other tracks are
    untouched, same "one track ripples at a time" rule ``ripple_delete``
    documents."""
    require_kind(doc, "timeline")
    content = copy.deepcopy(doc["content"])
    track = _track(content, op.get("track_id"))
    _require_not_locked(track)

    clip = op.get("clip")
    if not isinstance(clip, dict):
        raise InvalidOperation("timeline.ripple_insert requires an object 'clip'")
    required = ("id", "asset_ref", "timeline_start_ticks", "source_range",
                "source_clock", "timeline_duration_ticks")
    for key in required:
        if key not in clip:
            raise InvalidOperation(f"timeline.ripple_insert clip.{key} is required")
    if any(c.get("id") == clip["id"] for c in track["clips"]):
        raise InvalidOperation(f"clip id already exists on this track: {clip['id']!r}")

    new_range = _clip_range(clip)
    TimeRange.from_dict(clip["source_range"], field_name=f"clip {clip['id']!r}.source_range")
    Rational.from_dict(clip["source_clock"], field_name=f"clip {clip['id']!r}.source_clock")

    shifted = []
    for c in track["clips"]:
        c = copy.deepcopy(c)
        c_range = _clip_range(c)
        if c_range.start_ticks >= new_range.start_ticks:
            c["timeline_start_ticks"] = str(c_range.start_ticks + new_range.duration_ticks)
        shifted.append(c)
    shifted.append(copy.deepcopy(clip))
    _check_no_overlap(shifted)
    track["clips"] = shifted
    return content


OPS = {
    "timeline.insert_clip": insert_clip,
    "timeline.trim": trim,
    "timeline.split": split,
    "timeline.move": move,
    "timeline.ripple_delete": ripple_delete,
    "timeline.set_gain": set_gain,
    "timeline.snap": snap,
    "timeline.retime": retime,
    "timeline.add_marker": add_marker,
    "timeline.ripple_insert": ripple_insert,
}
