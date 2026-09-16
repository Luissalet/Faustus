"""Rational clocks and CFR/VFR conversion — WP13.

Closure criterion (``WP13.md``): "Cortes no acumulan error float" and "VFR
se indexa por PTS o proxy documentado". Both are satisfied the same way:
every conversion goes through :class:`fractions.Fraction` exactly, and a
variable-frame-rate source is represented by an explicit, ordered table of
presentation timestamps (:class:`PtsIndex`) rather than an assumed-constant
frame duration — never averaged into a float fps.

Common named rates are provided as convenience constants; nothing here
requires using them, any positive ``(numerator, denominator)`` pair works.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Literal, Sequence

from ..errors import InvalidOperation
from ..ops.model import Rational, ticks_str

# ── common rates (all exact) ────────────────────────────────────────────

NTSC_30000_1001 = Rational(30000, 1001)   # 29.97 fps
NTSC_60000_1001 = Rational(60000, 1001)   # 59.94 fps
FILM_24 = Rational(24, 1)
PAL_25 = Rational(25, 1)
FPS_30 = Rational(30, 1)
FPS_60 = Rational(60, 1)
AUDIO_44_1K = Rational(44100, 1)
AUDIO_48K = Rational(48000, 1)


def convert_ticks(ticks: int, from_clock: Rational, to_clock: Rational,
                   *, rounding: Literal["floor", "ceil", "nearest"] = "floor") -> int:
    """Re-expresses ``ticks`` (at ``from_clock`` ticks/second) as a tick
    count at ``to_clock`` ticks/second, exactly via :class:`Fraction` all
    the way through — the only place a whole tick is chosen is the final
    rounding, controlled explicitly by ``rounding`` (never an implicit
    ``int()`` truncation the caller did not ask for).

    A hundred consecutive conversions of the same nominal duration therefore
    never accumulate drift beyond what a single ``rounding`` step at the end
    introduces — see ``test_timeline_clocks.py`` for the 100-cut fixture the
    closure criterion asks for.
    """
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise InvalidOperation("ticks must be a non-negative integer")
    exact = Fraction(ticks) * to_clock.as_fraction() / from_clock.as_fraction()
    return _round_fraction(exact, rounding)


def _round_fraction(value: Fraction, rounding: str) -> int:
    if rounding == "floor":
        return value.numerator // value.denominator
    if rounding == "ceil":
        q, r = divmod(value.numerator, value.denominator)
        return q + (1 if r else 0)
    if rounding == "nearest":
        # value + 1/2, floored — exact in Fraction arithmetic, no float.
        return _round_fraction(value + Fraction(1, 2), "floor")
    raise InvalidOperation(f"unknown rounding policy: {rounding!r}")


@dataclass(frozen=True)
class MasterClock:
    """The document's own ``clock`` (``content.clock`` in a ``timeline``
    document), plus the conversions every other module in this package
    needs against it."""

    rate: Rational

    @classmethod
    def from_dict(cls, d: object) -> "MasterClock":
        return cls(Rational.from_dict(d, field_name="timeline.content.clock"))

    def to_dict(self) -> dict:
        return self.rate.to_dict()

    def seconds_of(self, ticks: int) -> Fraction:
        return self.rate.seconds_of(ticks)

    def ticks_of_seconds(self, seconds: Fraction, *, rounding: str = "floor") -> int:
        exact = seconds * self.rate.as_fraction()
        return _round_fraction(exact, rounding)

    def convert_from(self, ticks: int, source: Rational, *, rounding: str = "floor") -> int:
        """``ticks`` at ``source`` rate → ticks at this master clock's rate."""
        return convert_ticks(ticks, source, self.rate, rounding=rounding)

    def convert_to(self, ticks: int, target: Rational, *, rounding: str = "floor") -> int:
        """Ticks at this master clock's rate → ticks at ``target`` rate."""
        return convert_ticks(ticks, self.rate, target, rounding=rounding)


@dataclass(frozen=True)
class PtsIndex:
    """A variable-frame-rate source indexed by an explicit, strictly
    increasing table of per-frame presentation timestamps, at ``clock``
    ticks/second. This is the "proxy documented" the closure criterion
    requires instead of an assumed constant frame duration: a caller asking
    "what frame is at tick T" gets an answer derived from the real frame
    boundaries that were actually probed (e.g. via ``ffprobe``), not from
    ``T / average_frame_duration``.
    """

    clock: Rational
    pts_ticks: Sequence[int]  # frame i's presentation timestamp, strictly increasing

    def __post_init__(self) -> None:
        pts = list(self.pts_ticks)
        if not pts:
            raise InvalidOperation("PtsIndex requires at least one frame")
        for i, v in enumerate(pts):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise InvalidOperation(f"pts_ticks[{i}] must be a non-negative integer")
            if i > 0 and v <= pts[i - 1]:
                raise InvalidOperation("pts_ticks must be strictly increasing (VFR frames are ordered)")
        object.__setattr__(self, "pts_ticks", tuple(pts))

    @property
    def frame_count(self) -> int:
        return len(self.pts_ticks)

    def frame_at_tick(self, tick: int, *, policy: Literal["floor", "nearest"] = "floor") -> int:
        """Index of the frame presented at or before ``tick`` (``floor``),
        or whichever frame boundary is numerically closest (``nearest``).
        The policy is always explicit — never silently assumed — per the
        arch doc's "VFR ... política explícita al convertir a CFR"."""
        pts = self.pts_ticks
        if policy == "floor":
            idx = bisect_right(pts, tick) - 1
            return max(0, idx)
        if policy == "nearest":
            idx = bisect_left(pts, tick)
            if idx == 0:
                return 0
            if idx >= len(pts):
                return len(pts) - 1
            before, after = pts[idx - 1], pts[idx]
            return idx - 1 if (tick - before) <= (after - tick) else idx
        raise InvalidOperation(f"unknown VFR policy: {policy!r}")

    def tick_of_frame(self, frame_index: int) -> int:
        if not (0 <= frame_index < len(self.pts_ticks)):
            raise InvalidOperation(f"frame_index out of range: {frame_index}")
        return self.pts_ticks[frame_index]

    def to_cfr(self, target_rate: Rational, *, policy: Literal["floor", "nearest"] = "nearest") -> List[int]:
        """Explicit VFR→CFR resample: for each output tick at ``target_rate``
        spanning this source's duration, the source frame chosen by
        ``policy``. Returns the list of chosen source-frame indices, one per
        output tick — the caller decides what to do with repeats/drops
        (never hidden by this function)."""
        last = self.pts_ticks[-1]
        out_ticks = convert_ticks(last, self.clock, target_rate, rounding="ceil") + 1
        chosen: List[int] = []
        for out_tick in range(out_ticks):
            src_tick = convert_ticks(out_tick, target_rate, self.clock, rounding="floor")
            chosen.append(self.frame_at_tick(src_tick, policy=policy))
        return chosen


def clip_duration_seconds(ticks: int, clock: Rational) -> str:
    """Convenience for UI/debug surfaces: an exact decimal-ish string for a
    duration, without ever materializing a float. Kept as a ``Fraction``
    repr split into whole/remainder so it stays exact; callers that want a
    display value should format ``Fraction`` themselves."""
    frac = clock.seconds_of(ticks)
    whole, rem = divmod(frac.numerator, frac.denominator)
    return f"{whole}+{rem}/{frac.denominator}" if rem else str(whole)


__all__ = [
    "NTSC_30000_1001", "NTSC_60000_1001", "FILM_24", "PAL_25", "FPS_30", "FPS_60",
    "AUDIO_44_1K", "AUDIO_48K", "convert_ticks", "MasterClock", "PtsIndex",
    "clip_duration_seconds", "ticks_str",
]
