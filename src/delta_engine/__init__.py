"""Universal Delta Engine: what changed, what was asked for, and how we know.

`inspiration/PLAN_UNIVERSAL_DELTA_ENGINE_FAUSTUS.md`, phases 0 to 2. The
subsystem takes two IMMUTABLE revisions and a FROZEN intent and answers, for
one domain at a time: what the user asked to change, what changed to achieve
it, what changed that nobody asked for, what regressed, what was preserved --
and, the answer that makes the other five worth reading, what could not be
compared at all.

The map, in the order a comparison walks it:

    contracts.py     the shapes and the closed vocabularies
    intent.py        a request, frozen before anything is read
    invariants.py    what must hold, and where the requirement came from
    sources.py       a RevisionRef -> bytes, or an honest refusal
    adapters/        one per domain: what was OBSERVED, with method and tier
    alignment.py     which source element is which target element
    classification.py what an observation MEANS against the frozen intent
    confidence.py    how much to believe one finding, and how it propagates
    coverage.py      how much could be compared -- not the same question
    verdict.py       matched | partial | mismatched | regressed | inconclusive
    persistence.py   the store, and the §22 cache
    events.py        the live stream
    registry.py      which domains this machine can actually compare
    service.py       the order of all of the above, which is the guarantee
    integrations/    one-way adapters to prove, ChangeSets and the rest

Importing `persistence` HERE, at package level, is deliberate and is the fix
for a failure mode this repository has hit before: the schema is registered as
a side effect of importing that module, and if the only importer were a route
sitting behind a feature flag that is off, the tables would never be created on
a machine where the flag was never switched on. The State Mirror's package does
the same thing for the same reason.
"""

from __future__ import annotations

from src.delta_engine import contracts as contracts  # noqa: F401
from src.delta_engine import persistence as persistence  # noqa: F401  (schema)

__all__ = ["contracts", "persistence"]
