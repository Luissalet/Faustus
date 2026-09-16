"""Timeline validation report — WP13.

Closure criterion "Editar sólo cambia revisión y no renderiza sin orden":
before any export/render, a caller runs :func:`validate` and refuses to
proceed on anything but an ``ok`` report. This is a READ, never mutates the
document; ``src/creator/ops/timeline_ops.py`` ops already re-validate their
own narrow slice inline (per-track overlap) on every write — this module
gives the FULL picture across the whole document, with one finding per
problem and an explicit ``target`` (track/clip/marker id) so a UI can jump
straight to it instead of a caller re-parsing a message string.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation
from ..ops.model import Rational
from . import tracks as tracks_mod

Severity = str  # "error" | "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    target: Optional[str] = None  # a track id, "track:clip", or "marker:id"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.target is not None:
            d["target"] = self.target
        return d


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "findings": [f.to_dict() for f in self.findings]}


def validate(content: Dict[str, Any]) -> ValidationReport:
    """Never raises for a document-shaped problem this function itself
    finds — those become ``error`` findings. It DOES let a structurally
    malformed ``content`` (not even the right top-level shape) propagate as
    :class:`InvalidOperation`, since there is no well-formed target to
    attach a finding to at that point; callers already run
    ``documents.validate_content`` before anything reaches here in the
    normal write path, so that case is store-layer, not editorial."""
    findings: List[Finding] = []

    clock = Rational.from_dict(content.get("clock"), field_name="timeline.content.clock")
    tracks = tracks_mod.get_tracks(content)
    if not tracks:
        findings.append(Finding("error", "no_tracks", "a timeline document needs at least one track"))

    seen_track_ids = set()
    for track in tracks:
        tid = track.get("id")
        if tid in seen_track_ids:
            findings.append(Finding("error", "duplicate_track_id", f"duplicate track id: {tid!r}", target=str(tid)))
        seen_track_ids.add(tid)

        try:
            tracks_mod.validate_track(track)
        except InvalidOperation as exc:
            findings.append(Finding("error", "track_overlap", str(exc), target=str(tid)))

        seen_clip_ids = set()
        for clip in track.get("clips") or []:
            cid = clip.get("id")
            target = f"{tid}:{cid}"
            if cid in seen_clip_ids:
                findings.append(Finding("error", "duplicate_clip_id", f"duplicate clip id: {cid!r}", target=target))
            seen_clip_ids.add(cid)
            try:
                clip_range = tracks_mod.clip_range(clip)
                src_range = tracks_mod.clip_source_range(clip)
                src_clock = tracks_mod.clip_source_clock(clip)
            except InvalidOperation as exc:
                findings.append(Finding("error", "malformed_clip", str(exc), target=target))
                continue
            if src_clock.numerator <= 0 or src_clock.denominator <= 0:
                findings.append(Finding("error", "bad_source_clock", "source_clock must be positive", target=target))
            if src_range.duration_ticks <= 0:
                findings.append(Finding("error", "empty_source_range", "source_range.duration_ticks must be > 0", target=target))
            if clip_range.duration_ticks <= 0:
                findings.append(Finding("error", "empty_timeline_range", "timeline_duration_ticks must be > 0", target=target))

    try:
        markers = tracks_mod.get_markers(content)
        tracks_mod.validate_markers(content)
    except InvalidOperation as exc:
        findings.append(Finding("error", "bad_markers", str(exc)))
        markers = []

    duration = content.get("duration_ticks")
    if isinstance(duration, str) and duration.isdigit():
        dur = int(duration)
        for track in tracks:
            for clip in track.get("clips") or []:
                try:
                    r = tracks_mod.clip_range(clip)
                except InvalidOperation:
                    continue
                if r.end_ticks > dur:
                    findings.append(Finding(
                        "warning", "clip_exceeds_duration",
                        f"clip extends to {r.end_ticks} past duration_ticks {dur}",
                        target=f"{track.get('id')}:{clip.get('id')}",
                    ))
        for m in markers:
            tick = tracks_mod.marker_tick(m)
            if tick > dur:
                findings.append(Finding(
                    "warning", "marker_exceeds_duration",
                    f"marker at {tick} is past duration_ticks {dur}", target=f"marker:{m.get('id')}",
                ))

    ok = not any(f.severity == "error" for f in findings)
    return ValidationReport(ok=ok, findings=findings)


__all__ = ["Severity", "Finding", "ValidationReport", "validate"]
