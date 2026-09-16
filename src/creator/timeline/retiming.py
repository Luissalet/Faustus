"""Retiming maps — WP13, over WP12's ``ops.model.RetimingMap``.

A :class:`~src.creator.ops.model.RetimingMap` is an ordered list of
``(source TimeRange, dest TimeRange, mappable)`` segments. This module adds
the one operation that structure exists for: given a tick in one side, find
the corresponding tick (or declared cut/unmappable zone) on the other side
— ``map_time`` — plus a bounded notion of a clip nested inside another clip
through such a map (a "speed ramp" or a "cut-out-the-middle" edit is just a
retiming map with more than one segment).

"Clips anidados acotados" (WP13.md): nesting is bounded to ONE level and
the nested clip's own source is a plain (asset_ref, source_range,
source_clock) — never another timeline document. A retiming map can point
into that nested clip's source ticks, but this module does not resolve a
document graph; a caller wanting deeper nesting builds it explicitly one
level at a time. This keeps ``map_time`` total and side-effect free instead
of needing to fetch other documents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..errors import InvalidOperation
from ..ops.model import RetimingMap, RetimingSegment, TimeRange


@dataclass(frozen=True)
class MapResult:
    """Result of mapping one tick through a :class:`RetimingMap`."""

    source_ticks: Optional[int]  # None when the destination tick falls in an unmappable zone
    segment: RetimingSegment
    is_cut_boundary: bool  # True when the queried tick lands exactly on a segment edge


def map_time(retiming: RetimingMap, dest_ticks: int, *, direction: str = "dest_to_source") -> MapResult:
    """Maps ``dest_ticks`` (in the map's destination clock) to the source
    clock, or the reverse when ``direction="source_to_dest"``.

    Linear interpolation WITHIN a segment (the map's precision is one
    segment per constant-rate stretch — a caller wanting curved speed
    ramps builds them as many short segments, this function does not curve
    fit). A tick outside every segment, or inside a ``mappable=False``
    segment, returns ``source_ticks=None`` — never a guessed/clamped value,
    per SUB10's "nunca queda apuntando al audio eliminado".
    """
    if direction not in ("dest_to_source", "source_to_dest"):
        raise InvalidOperation(f"unknown retiming direction: {direction!r}")
    if isinstance(dest_ticks, bool) or not isinstance(dest_ticks, int) or dest_ticks < 0:
        raise InvalidOperation("dest_ticks must be a non-negative integer")

    for seg in retiming.segments:
        probe = seg.dest if direction == "dest_to_source" else seg.source
        other = seg.source if direction == "dest_to_source" else seg.dest
        if probe.start_ticks <= dest_ticks < probe.end_ticks:
            is_edge = dest_ticks == probe.start_ticks
            if not seg.mappable:
                return MapResult(source_ticks=None, segment=seg, is_cut_boundary=is_edge)
            offset = dest_ticks - probe.start_ticks
            # Scale the offset by the segment's own local rate (other/probe
            # duration ratio), exact integer arithmetic, floored to a tick —
            # never a float multiply.
            scaled = (offset * other.duration_ticks) // probe.duration_ticks
            scaled = min(scaled, other.duration_ticks - 1)
            return MapResult(source_ticks=other.start_ticks + scaled, segment=seg, is_cut_boundary=is_edge)
    raise InvalidOperation(f"{dest_ticks} ticks falls outside every segment of this retiming map")


def cut_boundaries(retiming: RetimingMap, *, side: str = "dest") -> list:
    """Every segment edge on ``side`` ("dest" or "source") — the discrete
    points a cut/speed-change happens, for snapping and for SUB10's
    "un corte dentro de un cue se divide" check."""
    if side not in ("dest", "source"):
        raise InvalidOperation(f"unknown side: {side!r}")
    points = set()
    for seg in retiming.segments:
        rng: TimeRange = seg.dest if side == "dest" else seg.source
        points.add(rng.start_ticks)
        points.add(rng.end_ticks)
    return sorted(points)


def unmappable_ranges(retiming: RetimingMap, *, side: str = "dest") -> list:
    """The declared cut/unmappable zones, as a list of ``TimeRange`` on
    ``side`` — what SUB10 needs to flag a discontinuity for editorial
    review instead of silently pointing at removed source material."""
    if side not in ("dest", "source"):
        raise InvalidOperation(f"unknown side: {side!r}")
    out = []
    for seg in retiming.segments:
        if not seg.mappable:
            out.append(seg.dest if side == "dest" else seg.source)
    return out


__all__ = ["MapResult", "map_time", "cut_boundaries", "unmappable_ranges", "RetimingMap", "RetimingSegment"]
