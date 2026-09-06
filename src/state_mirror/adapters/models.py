"""state_mirror/adapters/models.py -- which models are resident, and where.

`model_state.v1` for the models Ollama currently holds in memory. One
observation per loaded model, stamped with the moment the numbers behind it
were really collected.

**This adapter starts nothing.** Every route to `ollama /api/ps` in this
repository is a coroutine -- `system_usage_routes.collect_usage` and
`_collect_ollama` both -- and `gpu_placement.report` needs the list of loaded
models before it can say anything at all. A synchronous sweep that wanted
those numbers would have to start an event loop, and a sweep is called from
wherever the scheduler is, which sooner or later is inside a loop that is
already running. So this adapter reads the document `collect_usage` leaves in
its own cache and does not collect anything itself.

That is a real limitation and it is visible rather than hidden: the
observation is stamped with the cached document's own `ts`, so a machine
nobody has asked about in ten minutes produces observations that `freshness`
rates `stale`, and a consumer that needs current numbers is told to refresh
instead of being handed old ones dressed as new. With nothing cached at all --
a process where nothing has ever called `/api/system/usage` -- there is
nothing to observe and this adapter returns an empty list.

**A model absent from `/api/ps` is not observed here.** Ollama's ps lists what
is RESIDENT. It is not a catalogue, so this adapter never says `loaded=False`
about a model: it would be saying something about a model it cannot see.

The fields nothing here can fill are named in `UNOBSERVED_FIELDS`, with the
reason for each.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    iso_or_blank,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

__all__ = ["ModelsAdapter", "AVAILABILITIES", "UNOBSERVED_FIELDS",
           "SOURCE", "SCHEMA", "BACKEND"]

SOURCE = "models"
SCHEMA = "model_state.v1"

#: What `model_state.v1.availability` may say. The same three words
#: `capability_registry.STATES` uses, deliberately, so that two subsystems
#: describing the same machine do not need a translation table between them.
#: Only `available` is ever written from here: a model in `/api/ps` is resident
#: and answering, and nothing in that document says anything about a model
#: that is not in it.
AVAILABILITIES: Tuple[str, ...] = ("available", "unavailable", "unknown")

#: The only runner this adapter can see. Ollama is what `/api/ps` describes;
#: an OpenAI-compatible endpoint configured in `model_endpoints` is a
#: connection, and `connections.py` observes those.
BACKEND = "ollama"

#: What every loaded model has in common with every other, so the placement
#: strings stay a closed set rather than whatever a future field happens to
#: carry. `gpu_placement.describe` mints exactly these four.
PLACEMENTS: Tuple[str, ...] = ("single", "split", "cpu", "unknown")

#: `model_state.v1` fields no synchronous source on this box can fill.
UNOBSERVED_FIELDS: Dict[str, str] = {
    "active_requests": "ollama /api/ps reports what is resident, not what is "
                       "in flight, and nothing else on this box counts a "
                       "model's open requests",
    "tokens_per_second": "measured per generation by the caller that ran it; "
                         "no store keeps a model's last rate, so there is "
                         "nothing to read back",
}


def read_usage() -> Optional[Dict[str, Any]]:
    """The last `/api/system/usage` document, or None when there is not one.

    A plain dictionary read, no lock and no await: `collect_usage` writes the
    whole document into its cache in one assignment, so a reader either sees
    the previous document or the new one and never half of either.
    """
    from routes.system_usage_routes import _cache

    data = _cache.get("data")
    return data if isinstance(data, dict) else None


def collected_at(usage: Dict[str, Any]) -> str:
    """When `collect_usage` took these numbers, as an ISO timestamp.

    The document carries its own epoch `ts`. Using it rather than the sweep's
    clock is the whole reason this adapter is honest about a cache: an
    observation stamped now would make a ten-minute-old reading look like it
    had just been taken, and `freshness` would agree with it. `iso_or_blank`
    answers `""` for a document with no readable `ts`, and the caller treats
    that as nothing to observe rather than as now.
    """
    return iso_or_blank(usage.get("ts"))


def _whole(value: Any) -> Optional[int]:
    """A non-negative whole number, or None for anything else.

    None rather than zero, because `size_vram: null` and `size_vram: 0` are
    "ollama did not say" and "none of it is on the card", and a model running
    entirely on the CPU is a thing somebody needs to be able to see.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number >= 0 else None


def _shared_bytes(usage: Dict[str, Any], loaded: int) -> Optional[int]:
    """Ollama's shared GPU memory, but only when one model can own it.

    `gpu_shared_memory` counts the WDDM shared bytes for the Ollama server, not
    for a model. With one model resident the two are the same number and it is
    worth having -- spilling into shared memory is a twentyfold slowdown with
    every other gauge still green. With two resident there is no way to split
    it, and dividing it or attributing all of it to each would be inventing a
    per-model figure the counters never produced.
    """
    if loaded != 1:
        return None
    block = usage.get("gpu_mem")
    if not isinstance(block, dict) or not block.get("supported"):
        return None
    ollama = block.get("ollama")
    if not isinstance(ollama, dict):
        return None
    return _whole(ollama.get("shared"))


def _state_from(row: Dict[str, Any], shared: Optional[int]) -> Dict[str, Any]:
    """The `model_state.v1` body for one row of `usage["ollama"]["models"]`."""
    state: Dict[str, Any] = {
        "availability": "available",
        "loaded": True,
        "backend": BACKEND,
    }
    vram = _whole(row.get("size_vram"))
    if vram is not None:
        state["vram_bytes"] = vram
    context = _whole(row.get("context_length"))
    if context:
        state["context_capacity"] = context
    if shared is not None:
        state["shared_memory_bytes"] = shared
    expires = str(row.get("expires_at") or "").strip()
    if expires:
        state["expires_at"] = expires
    placement = str(row.get("placement") or "").strip()
    if placement in PLACEMENTS:
        state["placement"] = placement
    return state


def loaded_models(usage: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The rows of `/api/ps` the usage document carries, named and deduplicated.

    An unreachable Ollama produces an empty list here rather than an error:
    `_collect_ollama` returns `reachable: False` with no models, and having
    nothing to say about any model is the correct reading of that.
    """
    block = usage.get("ollama")
    if not isinstance(block, dict) or not block.get("reachable"):
        return []
    rows: List[Dict[str, Any]] = []
    seen = set()
    for row in block.get("models") or ():
        name = str((row or {}).get("name") or "").strip() if isinstance(row, dict) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append(row)
    return rows


class ModelsAdapter(ThreadedAdapter):
    """Models resident in Ollama, as `model_state.v1` rows."""

    name = SOURCE
    schemas = (SCHEMA,)

    def _rows(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """`(model name, observed_at, state)` for every resident model."""
        usage = self._safe(read_usage, default=None)
        if not usage:
            return []
        observed_at = self._safe(collected_at, usage, default="") or ""
        if not observed_at:
            # A document with no readable collection time cannot be aged, and
            # an observation that cannot be aged is one nothing may act on.
            # Saying nothing is the honest reading of an undatable reading.
            return []
        models = self._safe(loaded_models, usage, default=()) or ()
        shared = self._safe(_shared_bytes, usage, len(models), default=None)
        return [(str(row.get("name") or "").strip(), observed_at,
                 _state_from(row, shared)) for row in models]

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for name, _observed_at, _state in self._safe(self._rows, default=()) or ():
            row = self._safe(entity, "model", name, scope=scope, schema=SCHEMA,
                             display_name=name, labels=(BACKEND,), default=None)
            if row is not None:
                out.append(row)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        out: List[StateObservation] = []
        for name, observed_at, state in self._safe(self._rows, default=()) or ():
            target = self._safe(entity_id, "model", scope.owner, name,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            obs = observation(
                target, SOURCE, state,
                scope=scope,
                schema=SCHEMA,
                # Ollama is the authority on what Ollama has loaded, and every
                # value here is what it answered.
                epistemic="observed",
                observed_at=observed_at,
                # `/api/ps` sees every resident model but not every model, and
                # `model_state.v1` is not a snapshot schema for that reason.
                partial=True,
            )
            if obs is not None:
                out.append(obs)
        return out
