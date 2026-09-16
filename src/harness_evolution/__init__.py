"""harness_evolution — versioned-transaction evolution of the agent harness
(dictamen §7): propose a typed `CandidatePatch` against a `HarnessRevision`,
validate it before it is ever available, evaluate it against source AND
held-out tasks, promote it by compare-and-swap with a bounded canary, and
recheck/revert it if the capabilities it depended on move under it.

See `service.HarnessEvolutionService` for the orchestrated entry point;
`models`/`store`/`validator`/`evaluator`/`promotion` are the pieces it
coordinates.
"""
from .models import CandidatePatch, HarnessRevision
from .service import HarnessEvolutionService
from .store import HarnessEvolutionStore, StaleParent

__all__ = ["CandidatePatch", "HarnessRevision", "HarnessEvolutionService",
          "HarnessEvolutionStore", "StaleParent"]
