"""Shared value types for typed document operations (WP12).

``docs/spec/creator/plan/docs/03_ARQUITECTURA_Y_CONTRATOS.md`` § "Tiempo,
regiones y operaciones" sets three rules this module exists to satisfy:

* Times are non-negative integers expressed at an explicit ticks-per-second
  rate, both serialized as decimal strings — never a JS ``number`` (loses
  precision above 2**53-1) and never a bare float second count (cannot
  represent 30000/1001 NTSC or a 48000 Hz sample boundary exactly).
* Image regions carry an explicit coordinate system (source dimensions,
  applied EXIF orientation, layer transform) — bare x/y from a UI capture
  cannot be reproduced later.
* Edits are expressed over an ``object_id`` (+ the document's revision,
  enforced by ``DocumentStore.apply_command``'s optimistic concurrency),
  never an implicit "the previous image/clip".

Kept dependency-free (stdlib ``fractions`` only) so it has no opinion about
sqlite, HTTP or the document store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

from ..errors import InvalidOperation


def _parse_ticks(value: Any, *, field_name: str, allow_zero: bool) -> int:
    """Accept the wire format (decimal string, no leading zero, no sign)
    or a plain non-negative int for convenience when a handler builds a
    dict in Python. Rejects float and bool (``bool`` is an ``int``
    subclass in Python and would otherwise silently pass)."""
    if isinstance(value, bool):
        raise InvalidOperation(f"{field_name} must be an integer, not a bool")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value and (value == "0" or (value[0] != "0" and value.isdigit())):
        n = int(value)
    else:
        raise InvalidOperation(
            f"{field_name} must be a non-negative integer string (no leading zero)"
        )
    if n < 0:
        raise InvalidOperation(f"{field_name} must be >= 0")
    if not allow_zero and n == 0:
        raise InvalidOperation(f"{field_name} must be > 0")
    return n


def ticks_str(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise InvalidOperation("tick counts must be a non-negative integer")
    return str(n)


@dataclass(frozen=True)
class Rational:
    """A ticks-per-second rate, kept as an exact ``(numerator, denominator)``
    pair of positive integers — e.g. 30000/1001 (NTSC 29.97 fps) or
    48000/1 (48 kHz audio). Arithmetic goes through :mod:`fractions` so
    nothing is ever rounded to a float."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise InvalidOperation("Rational numerator/denominator must both be > 0")

    @classmethod
    def from_dict(cls, d: Any, *, field_name: str = "clock") -> "Rational":
        if not isinstance(d, dict):
            raise InvalidOperation(f"{field_name} must be an object")
        num = _parse_ticks(d.get("ticks_per_second_numerator"),
                            field_name=f"{field_name}.ticks_per_second_numerator", allow_zero=False)
        den = _parse_ticks(d.get("ticks_per_second_denominator"),
                            field_name=f"{field_name}.ticks_per_second_denominator", allow_zero=False)
        return cls(num, den)

    def to_dict(self) -> Dict[str, str]:
        return {
            "ticks_per_second_numerator": ticks_str(self.numerator),
            "ticks_per_second_denominator": ticks_str(self.denominator),
        }

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def seconds_of(self, ticks: int) -> Fraction:
        """Exact seconds for ``ticks`` ticks at this rate, as a ``Fraction``
        (ticks / (num/den) = ticks * den / num), never a lossy float until
        a caller explicitly asks for one."""
        return Fraction(int(ticks) * self.denominator, self.numerator)

    def ticks_of(self, seconds: Fraction) -> int:
        """Inverse of :meth:`seconds_of`, floored to a whole tick."""
        return int(seconds * self.numerator / self.denominator)


@dataclass(frozen=True)
class TimeRange:
    """A half-open ``[start_ticks, start_ticks + duration_ticks)`` interval
    at some (implicit, caller-tracked) tick rate."""

    start_ticks: int
    duration_ticks: int

    def __post_init__(self) -> None:
        if self.start_ticks < 0:
            raise InvalidOperation("start_ticks must be >= 0")
        if self.duration_ticks <= 0:
            raise InvalidOperation("duration_ticks must be > 0")

    @property
    def end_ticks(self) -> int:
        return self.start_ticks + self.duration_ticks

    def overlaps(self, other: "TimeRange") -> bool:
        return self.start_ticks < other.end_ticks and other.start_ticks < self.end_ticks

    @classmethod
    def from_dict(cls, d: Any, *, start_key: str = "start_ticks",
                  duration_key: str = "duration_ticks", field_name: str = "range") -> "TimeRange":
        if not isinstance(d, dict):
            raise InvalidOperation(f"{field_name} must be an object")
        start = _parse_ticks(d.get(start_key), field_name=f"{field_name}.{start_key}", allow_zero=True)
        dur = _parse_ticks(d.get(duration_key), field_name=f"{field_name}.{duration_key}", allow_zero=False)
        return cls(start, dur)

    def to_dict(self, *, start_key: str = "start_ticks", duration_key: str = "duration_ticks") -> Dict[str, str]:
        return {start_key: ticks_str(self.start_ticks), duration_key: ticks_str(self.duration_ticks)}


@dataclass(frozen=True)
class Region:
    """An image region with the coordinate system it was measured against,
    so a crop/mask/offset reproduces the same pixels on replay."""

    x: int
    y: int
    width: int
    height: int
    source_w: int
    source_h: int
    exif_orientation: int = 1
    layer_transform: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise InvalidOperation("Region width/height must be > 0")
        if self.source_w <= 0 or self.source_h <= 0:
            raise InvalidOperation("Region source_w/source_h must be > 0")
        if not (1 <= self.exif_orientation <= 8):
            raise InvalidOperation("Region exif_orientation must be in 1..8")
        if self.x < 0 or self.y < 0 or self.x + self.width > self.source_w or self.y + self.height > self.source_h:
            raise InvalidOperation("Region must lie inside its source dimensions")

    @classmethod
    def from_dict(cls, d: Any) -> "Region":
        if not isinstance(d, dict):
            raise InvalidOperation("region must be an object")
        try:
            return cls(
                x=int(d["x"]), y=int(d["y"]), width=int(d["width"]), height=int(d["height"]),
                source_w=int(d["source_w"]), source_h=int(d["source_h"]),
                exif_orientation=int(d.get("exif_orientation", 1)),
                layer_transform=d.get("layer_transform"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidOperation(f"region is malformed: {exc}") from exc

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "source_w": self.source_w, "source_h": self.source_h,
            "exif_orientation": self.exif_orientation,
        }
        if self.layer_transform is not None:
            out["layer_transform"] = self.layer_transform
        return out


@dataclass(frozen=True)
class RetimingSegment:
    """One piece of a retiming map: a source interval that lands at a
    destination interval, or a declared cut/unmappable zone."""

    source: TimeRange
    dest: TimeRange
    mappable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "dest": self.dest.to_dict(),
            "mappable": self.mappable,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "RetimingSegment":
        if not isinstance(d, dict):
            raise InvalidOperation("retiming segment must be an object")
        return cls(
            source=TimeRange.from_dict(d.get("source"), field_name="segment.source"),
            dest=TimeRange.from_dict(d.get("dest"), field_name="segment.dest"),
            mappable=bool(d.get("mappable", True)),
        )


@dataclass(frozen=True)
class RetimingMap:
    """An ordered set of source→destination interval mappings. Segments are
    kept in destination order; cuts/unmappable zones are represented as
    segments with ``mappable=False`` rather than silently dropped, so a
    consumer can see exactly what did not carry over."""

    segments: List[RetimingSegment] = field(default_factory=list)

    def __post_init__(self) -> None:
        prev_end: Optional[int] = None
        for seg in self.segments:
            if prev_end is not None and seg.dest.start_ticks < prev_end:
                raise InvalidOperation("retiming map destination segments must not overlap")
            prev_end = seg.dest.end_ticks

    @classmethod
    def from_list(cls, items: Any) -> "RetimingMap":
        if not isinstance(items, list):
            raise InvalidOperation("retiming map must be an array of segments")
        return cls([RetimingSegment.from_dict(it) for it in items])

    def to_list(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.segments]


def require_kind(doc: Dict[str, Any], expected: str) -> None:
    kind = doc.get("kind")
    if kind != expected:
        raise InvalidOperation(f"this op only applies to '{expected}' documents, got {kind!r}")


def find_by_id(items: List[Dict[str, Any]], item_id: Any, *, what: str) -> Dict[str, Any]:
    if not isinstance(item_id, str) or not item_id:
        raise InvalidOperation(f"object_id for {what} must be a non-empty string")
    for item in items:
        if item.get("id") == item_id:
            return item
    raise InvalidOperation(f"unknown {what} object_id: {item_id!r}")


def index_by_id(items: List[Dict[str, Any]], item_id: Any, *, what: str) -> int:
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            return i
    raise InvalidOperation(f"unknown {what} object_id: {item_id!r}")
