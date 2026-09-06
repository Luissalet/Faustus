"""What could actually be compared, and how to say what could not. §17.

`Coverage` is the contract; this module is the arithmetic around it. Its whole
job is to keep "we looked and it was fine" apart from "we did not look", which
is rule 1 of `contracts.py` wearing a different hat: an assertion says a
property was preserved, a coverage says the property was in the part of the
artifact anybody examined, and a delta that reports the first without the
second is the lie the subsystem exists to prevent.

Three decisions worth naming, because each one had an obvious wrong answer:

* **`merge` takes the minimum over the parts that REPORTED a dimension**, not
  over all parts. `Coverage.ratio` already distinguishes `None` (not measured)
  from `0.0` (measured, covered nothing), and treating a silent part as a zero
  would erase that distinction on the first merge: an image adapter that never
  looks at `temporal` is not a witness that no time was covered.

* **`sufficient` refuses a dimension nobody reported.** An unmeasured axis is
  not a passing axis. Same sentence as above, in the direction that matters to
  a caller about to act on the answer.

* **An unknown dimension name raises.** §17 closes `COVERAGE_DIMENSIONS` so
  that two adapters cannot invent `visual` and `pixels` for one thing; a
  `sufficient(..., dimensions=("visual",))` that quietly answered `False`
  would hide the typo behind a plausible refusal for as long as the code lives.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.delta_engine.contracts import (
    COVERAGE_DIMENSIONS,
    Coverage,
    DeltaError,
)

__all__ = ["build", "merge", "sufficient", "gaps"]


def build(*, source_readable: bool, target_readable: bool,
          dimensions: Optional[Mapping[str, float]] = None,
          regions: Sequence[str] = (), excluded: Sequence[str] = (),
          notes: Sequence[str] = ()) -> Coverage:
    """A `Coverage`, validated by the contract that owns it.

    Deliberately a payload handed to `Coverage.parse` rather than a call to the
    dataclass constructor: the constructor accepts anything, and the rejections
    that matter -- an unknown dimension name, a ratio of 1.4, a blank region --
    all live in `parse`. Building the object directly here would create a
    second door into the same contract, and the day the two disagree the caller
    who used the quiet door wins.

    Nothing is coerced on the way in. A `source_readable` of `1` is refused by
    `flag` naming the field, because reading a truthy value as a permission to
    call an end readable is exactly the guess rule 3 of `contracts/base.py`
    forbids.
    """
    payload: Dict[str, Any] = {
        "source_readable": source_readable,
        "target_readable": target_readable,
        "dimensions": dict(dimensions) if dimensions else {},
    }
    if regions:
        payload["regions_analyzed"] = list(regions)
    if excluded:
        payload["excluded"] = list(excluded)
    if notes:
        payload["notes"] = list(notes)
    return Coverage.parse(payload, "coverage")


def merge(parts: Sequence[Coverage]) -> Coverage:
    """One coverage out of several. Minimum per dimension, AND per readable.

    `AND` and not `or` for readability: a comparison is only as readable as its
    least readable end, and one adapter that opened the target does not make
    the source readable for the adapter that could not.

    An empty sequence returns `Coverage()`, whose two readable flags default to
    `False`. That is the honest floor and not an oversight: merging nothing has
    established nothing, and returning a full-coverage object for it would let
    a pipeline that ran no adapters report that everything was compared.
    """
    collected = tuple(parts)
    if not collected:
        return Coverage()

    dimensions: Dict[str, float] = {}
    for name in COVERAGE_DIMENSIONS:
        reported = [value for value in (part.ratio(name) for part in collected)
                    if value is not None]
        if reported:
            dimensions[name] = min(reported)

    return build(
        source_readable=all(part.source_readable for part in collected),
        target_readable=all(part.target_readable for part in collected),
        dimensions=dimensions,
        regions=_unique(name for part in collected for name in part.regions_analyzed),
        excluded=_unique(name for part in collected for name in part.excluded),
        notes=_unique(note for part in collected for note in part.notes),
    )


def sufficient(coverage: Coverage, *, minimum: float = 0.5,
               dimensions: Sequence[str] = ()) -> bool:
    """Whether this coverage supports a conclusion about `dimensions`.

    A dimension the caller named and nobody measured answers `False`, never
    `True`: `Coverage.ratio` returns `None` there, and reading `None` as "no
    limit found" is the same mistake as reading a detector's silence as
    preservation.

    With no dimensions named, every dimension that WAS reported has to meet the
    minimum, and a coverage reporting none at all is insufficient -- "both ends
    were readable and nothing was measured" is the shape of a delta that looked
    at nothing, and it must not be the shape of a `True`.
    """
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
        raise DeltaError("minimum", "expected a number between 0 and 1", got=minimum)
    if minimum < 0.0 or minimum > 1.0:
        raise DeltaError("minimum", "is outside 0..1", got=minimum)

    names: Tuple[str, ...] = tuple(dimensions) if dimensions else tuple(coverage.dimensions)
    for name in names:
        if name not in COVERAGE_DIMENSIONS:
            raise DeltaError(
                "dimensions",
                f"is not a known coverage dimension; known: {list(COVERAGE_DIMENSIONS)}",
                got=name,
            )
    if not coverage.both_readable:
        return False
    if not names:
        return False
    for name in names:
        ratio = coverage.ratio(name)
        if ratio is None or ratio < minimum:
            return False
    return True


def gaps(coverage: Coverage) -> Tuple[str, ...]:
    """Readable phrases for everything this comparison could not compare.

    This is what `verdict.explain` puts after "I could not check": §35's last
    clause, "no pude comprobar detalle fino del cabello por resolución
    insuficiente". The phrases are built from the fields rather than stored,
    for `UniversalDelta`'s reason -- a stored summary is a second source of
    truth about the same facts, and the day it drifts the reader believes the
    shorter one.

    A dimension at 1.0 produces no phrase, an unreported dimension produces
    none either, and those two silences mean different things: the first is
    "all of it", the second is "nobody measured this axis". Only the first is
    the absence of a gap, so an unreported dimension is not listed here and is
    instead what `sufficient` refuses -- naming every axis no adapter claimed
    would bury the ones that were tried and fell short.
    """
    out: List[str] = []
    if not coverage.source_readable:
        out.append("the source revision could not be read")
    if not coverage.target_readable:
        out.append("the target revision could not be read")
    for name in COVERAGE_DIMENSIONS:
        ratio = coverage.ratio(name)
        if ratio is not None and ratio < 1.0:
            out.append(f"{name} coverage was {ratio:g} of the comparison")
    for name in coverage.excluded:
        out.append(f"excluded from the comparison: {name}")
    out.extend(coverage.notes)
    return _unique(out)


def _unique(values: Any) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists this module CONCATENATES.

    Not a politeness applied to someone else's data: `merge` joins the regions
    of several adapters, and the same region examined twice is one region, not
    a duplicate that `text_list` should reject on the way into `Coverage`. The
    repeat is an artifact of the concatenation done here, so it is removed
    here, and a duplicate inside a single part is still the adapter's own
    contradiction and is still refused by `parse`.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text_value = str(value)
        if text_value in seen:
            continue
        seen.add(text_value)
        out.append(text_value)
    return tuple(out)
