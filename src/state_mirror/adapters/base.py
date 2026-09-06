"""The contract every source of state has to meet, and the registry of them.

An adapter is the only place in State Mirror that touches another subsystem,
and therefore the only place a fact enters the mirror at all. Everything
downstream -- `reducers`, `persistence`, the queries and the routes -- is pure
or owns its own storage, so the honesty of the whole subsystem rests on what
these classes promise. Three promises, each named with the failure it prevents:

**A read never raises.** `discover()` and `observe()` are called by a sweep that
walks every adapter in turn. One registry that was renamed, one sqlite file
that will not open, one row carrying `None` where a string was expected: any of
those inside a bare loop takes the whole sweep down, and the mirror then
reports nothing at all about a machine that is mostly working. `_safe` is how
that stays local, and `observation()` is why a schema typo in one adapter costs
one field instead of the sweep.

**An adapter never invents an owner.** Every entity id carries the owner the
sweep was run for, taken from the `Scope` and never from the row. Several of
the registries behind these adapters keep an `owner` of their own that means
something else entirely -- an objective's `owner` is the literal word `user` or
`agent`, a council seat's is a participant id -- and one of those reaching an
entity id would file one person's state under another person's name.

**The epistemology is stated per observation, not per field.** `reducers`
stamps `StateObservation.epistemic` onto every field it folds, so an adapter
with a status read out of a registry and a count it computed itself emits TWO
observations rather than one carrying an averaged claim. That is why the
builders below are cheap to call: an adapter is expected to call
`observation()` several times for one entity.

`ADAPTER_FACTORIES` lives in `adapters/__init__.py` and is imported here
lazily, inside `all_adapters()`. Importing this module must not pull in
sqlalchemy, the council store or the session database, and an adapter module
that fails to import has to cost itself rather than the package. The registry
is the same shape, and exists for the same reasons, as
`src/context_engine/candidates.py::register_source`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (Any, Callable, Dict, List, Mapping, Optional, Protocol,
                    Sequence, Tuple, runtime_checkable)

from src.state_mirror.contracts import (
    REAL_NAMESPACE,
    StateEntity,
    StateObservation,
    StateRelation,
    entity_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Scope",
    "StateAdapter",
    "ThreadedAdapter",
    "iso_or_blank",
    "entity",
    "observation",
    "relation",
    "register",
    "get",
    "registered",
    "all_adapters",
    "reset_adapters",
]


@dataclass(frozen=True)
class Scope:
    """Who a sweep is for, and how much of each registry to read.

    Frozen so an adapter cannot widen its own scope halfway through a sweep.
    `owner` is `""` on a single-user install and that is a REAL owner, not a
    missing one -- `contracts.entity_id` mints `run:///real/...` for it on
    purpose -- so nothing here may read `""` as "every owner". The registries
    that take an owner filter are handed this value verbatim; the ones that
    have no owner column are named in the module docstring of the adapter that
    reads them, because "this row belongs to nobody" and "this row belongs to
    you" must not become the same sentence by accident.
    """

    owner: str = ""
    project_id: str = ""
    workspace: str = ""
    namespace: str = REAL_NAMESPACE
    limit: int = 100

    def capped(self, ceiling: int = 500) -> int:
        """`limit` as a number a store query can be handed, whatever arrived.

        Total on purpose: the scope comes from a caller, and a sweep that died
        because somebody passed `limit="50"` would be a sweep that reports
        nothing about a machine that is fine.
        """
        try:
            wanted = int(self.limit)
        except (TypeError, ValueError):
            wanted = 100
        return max(1, min(wanted, max(1, int(ceiling))))


@runtime_checkable
class StateAdapter(Protocol):
    """What a sweep needs from a source of state, and nothing else.

    `schemas` is a promise rather than a filter: it lets a sweep that only
    wants `run_state.v1` skip the adapters that cannot produce one, without
    waking them first.
    """

    name: str
    schemas: Tuple[str, ...]

    def available(self) -> bool: ...

    def discover(self, scope: "Scope") -> List[StateEntity]: ...

    def observe(self, scope: "Scope") -> List[StateObservation]: ...

    #: Optional. `ThreadedAdapter` answers `[]`, which is the honest reply from
    #: an adapter reading a flat registry: it has nothing to say about how its
    #: rows relate to each other.
    def relations(self, scope: "Scope") -> List[StateRelation]: ...


class ThreadedAdapter:
    """The base every adapter in this package subclasses.

    Named after `context_engine.candidates.ThreadedSource`, whose bargain it
    keeps: the subclass writes ordinary blocking code and something above it
    decides which thread that runs on. It deliberately does NOT do the
    `asyncio.to_thread` hop that class does -- a State Mirror sweep is a
    background job and not a turn on the chat hot path, so there is no event
    loop here to protect, and adding one would only mean a test needs a running
    loop to read a dict.
    """

    name: str = ""
    #: The `contracts.STATE_SCHEMAS` this adapter produces. A tuple and not a
    #: sentence in a docstring, because a sweep routes on it.
    schemas: Tuple[str, ...] = ()

    def available(self) -> bool:
        return True

    def discover(self, scope: Scope) -> List[StateEntity]:
        return []

    def observe(self, scope: Scope) -> List[StateObservation]:
        return []

    def relations(self, scope: Scope) -> List[StateRelation]:
        return []

    def _safe(self, fn: Callable[..., Any], *args: Any,
              default: Any = None, **kwargs: Any) -> Any:
        """Call `fn`, and answer `default` instead of raising.

        The wrapper every read in this package goes through. `default` is
        keyword-only and is consumed here, so a callee with a `default`
        argument of its own has to be wrapped in a lambda -- a small cost
        against the alternative, which is a whole sweep lost because one
        registry changed a signature.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                               # noqa: BLE001
            logger.debug("state adapter %s: %s failed: %s",
                         self.name or type(self).__name__,
                         getattr(fn, "__name__", fn), exc)
            return default


# -- total builders --------------------------------------------------------
#
# Every one of these answers `None` rather than raising. They sit between an
# adapter and a contract that rejects loudly and correctly, and the whole point
# of the layer is that a rejection costs the row it is about. A `None` here is
# always logged with the value that caused it, so the culprit is named while
# the adapter that produced it is still on the stack.

def iso_or_blank(value: Any) -> str:
    """An ISO-8601 UTC string, or `""` when the value is not a time.

    Every registry dates its rows differently: `dispatch` and `bg_jobs` write
    `time.time()` floats, `media_runs` writes ISO strings, the council store
    writes its own stamp. `contracts` validates timestamps through
    `base.timestamp`, which RAISES on anything it cannot read, so one stray
    epoch int would cost a whole observation. Unreadable becomes "not
    recorded", which is exactly what `""` means to the contract.

    Not imported from `context_engine.candidates`, which has the same function
    for the same reason: the two subsystems are siblings, and a State Mirror
    sweep that could not run without the Context Engine importable would be a
    dependency nobody chose.
    """
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (moment.astimezone(timezone.utc).replace(microsecond=0)
                .isoformat().replace("+00:00", "Z"))
    if isinstance(value, (int, float)):
        try:
            return iso_or_blank(datetime.fromtimestamp(float(value), timezone.utc))
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        try:
            return iso_or_blank(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return ""
    return ""


def entity(kind: str, identifier: str, *, scope: Scope, display_name: str = "",
           labels: Sequence[str] = (), source_refs: Sequence[str] = (),
           schema: str = "", created_at: Any = "", updated_at: Any = "",
           sensitivity: str = "private") -> Optional[StateEntity]:
    """A `StateEntity` in this scope, or `None` when the contract refuses it.

    Owner and namespace are taken from the scope and are NOT parameters. An
    adapter that could choose them could file one owner's row under another's,
    and that is the one mistake in this package with a blast radius outside the
    mirror.
    """
    try:
        ident = entity_id(kind, scope.owner, identifier, namespace=scope.namespace)
        return StateEntity.parse({
            "id": ident,
            "kind": kind,
            "owner": scope.owner,
            "namespace": scope.namespace,
            "project_id": scope.project_id,
            "display_name": str(display_name or "")[:512],
            "labels": [str(x) for x in labels if str(x or "").strip()],
            "source_refs": [str(x) for x in source_refs if str(x or "").strip()],
            "schema": schema,
            "created_at": iso_or_blank(created_at),
            "updated_at": iso_or_blank(updated_at),
            "sensitivity": sensitivity,
        })
    except Exception as exc:                                   # noqa: BLE001
        logger.debug("state adapter: entity %s/%r rejected: %s",
                     kind, identifier, exc)
        return None


def observation(target: str, source: str, state: Mapping[str, Any], *,
                scope: Scope, epistemic: str = "observed", schema: str = "",
                observed_at: Any = "", valid_for_seconds: int = 0,
                partial: bool = True, evidence_refs: Sequence[str] = (),
                source_revision: str = "", sequence: int = 0,
                sensitivity: str = "private") -> Optional[StateObservation]:
    """A `StateObservation`, or `None` when the contract will not have it.

    Two things happen here that an adapter must not have to remember. A field
    whose value is `None` is DROPPED rather than written: absence is how a
    partial observation spells "I did not sample this", and a stored `None`
    would read as an observed nothing -- the exact collapse `contracts._flag`
    exists to prevent. And a body left empty after that drop produces no
    observation at all, because a look that saw no fields is not a look.

    `None` is also what a misspelled schema field produces. That is the point:
    an adapter must not be able to end a sweep with a typo, and the debug line
    below names the schema and the adapter that got it wrong.
    """
    body = {str(k): v for k, v in dict(state or {}).items() if v is not None}
    if not body:
        return None
    try:
        return StateObservation.parse({
            "entity_id": target,
            "owner": scope.owner,
            "project_id": scope.project_id,
            "schema": schema,
            "source": source,
            "epistemic": epistemic,
            "sequence": sequence,
            "observed_at": iso_or_blank(observed_at),
            "valid_for_seconds": valid_for_seconds,
            "partial": partial,
            "state": body,
            "evidence_refs": [str(x) for x in evidence_refs if str(x or "").strip()],
            "source_revision": str(source_revision or "")[:256],
            "sensitivity": sensitivity,
        })
    except Exception as exc:                                   # noqa: BLE001
        logger.debug("state adapter %s: observation on %s (%s) rejected: %s",
                     source, target, schema or "default schema", exc)
        return None


def relation(from_id: str, kind: str, to_id: str, *, source: str,
             scope: Optional[Scope] = None, origin: str = "observed",
             observed_at: Any = "") -> Optional[StateRelation]:
    """A `StateRelation`, or `None` when the contract refuses it.

    The commonest refusal is a dangling end: an edge to an id that is not an
    id. Dropping it is right -- an edge to nothing answers no query and is
    never noticed again -- and logging it is how the adapter that minted the
    bad end gets found.
    """
    try:
        return StateRelation.parse({
            "from_id": from_id,
            "to_id": to_id,
            "kind": kind,
            "owner": scope.owner if scope is not None else "",
            "origin": origin,
            "source": source,
            "observed_at": iso_or_blank(observed_at),
        })
    except Exception as exc:                                   # noqa: BLE001
        logger.debug("state adapter %s: relation %s -%s-> %s rejected: %s",
                     source, from_id, kind, to_id, exc)
        return None


# -- the registry ----------------------------------------------------------
#
# Process-wide, and not a parameter threaded through the sweep, for the reason
# `context_engine.candidates` gives about its own: adapters are stateful in the
# boring way (a memoised store handle, a council service built once), and
# rebuilding the set per sweep would throw that away every time.
# `reset_adapters()` exists so one test never inherits another's doubles.

_LOCK = threading.RLock()
_REGISTRY: "Dict[str, StateAdapter]" = {}
_DEFAULTS_BUILT = False


def _name_of(adapter: Any) -> str:
    return str(getattr(adapter, "name", "") or "").strip()


def register(adapter: "StateAdapter") -> None:
    """Add or replace an adapter by its `name`.

    Registering before the first `all_adapters()` call is the supported way to
    substitute a double: the defaults are only filled in for names nobody has
    claimed. This is a write, so it raises -- an adapter that silently failed
    to register would look exactly like one whose registry is empty.
    """
    name = _name_of(adapter)
    if not name:
        raise ValueError("a state adapter needs a non-empty name")
    with _LOCK:
        _REGISTRY[name] = adapter


def get(name: str) -> "Optional[StateAdapter]":
    with _LOCK:
        return _REGISTRY.get(str(name or "").strip())


def registered() -> Tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_REGISTRY))


def all_adapters() -> "List[StateAdapter]":
    """Every adapter, built once and memoised in the registry.

    The import of `.adapters` is deferred to the first call so that importing
    this module stays cheap for a process that only wanted `Scope`, and so that
    an adapter whose module fails to import costs that adapter rather than the
    sweep.
    """
    global _DEFAULTS_BUILT
    with _LOCK:
        if not _DEFAULTS_BUILT:
            for factory in _factories():
                try:
                    adapter = factory()
                except Exception as exc:                       # noqa: BLE001
                    logger.warning("state adapter factory %r failed: %s",
                                   factory, exc)
                    continue
                name = _name_of(adapter)
                if name:
                    _REGISTRY.setdefault(name, adapter)
            _DEFAULTS_BUILT = True
        return list(_REGISTRY.values())


def reset_adapters() -> None:
    """Forget every adapter, defaults included. Tests, and a reconfigured
    process (a new data dir, a new workspace)."""
    global _DEFAULTS_BUILT
    with _LOCK:
        _REGISTRY.clear()
        _DEFAULTS_BUILT = False


def _factories() -> "Tuple[Callable[[], StateAdapter], ...]":
    try:
        from src.state_mirror.adapters import ADAPTER_FACTORIES
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("state mirror adapters could not be imported: %s", exc)
        return ()
    return tuple(ADAPTER_FACTORIES)
