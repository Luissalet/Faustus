"""ledger.py — what the Council has agreed, claimed and still owes.

A room of models produces prose faster than anyone can audit it, and the
failure that follows is always the same: the closing text is written by a
model, so a decision nobody restated in the last round quietly disappears, an
objection that was never answered reads as resolved, and two participants edit
the same file because "I'll take that one" lived in a sentence instead of in a
lock. Section 6.3 of the plan draws the line this module implements: the
LEDGER -- not the transcript, and not a summariser -- decides what is agreed,
claimed or pending.

Three rules have teeth here:

* One resource, one owner. `request_claim` refuses a second holder and hands
  back the claim that already exists, and equivalent spellings of a path
  (``src/a.py``, ``src\\a.py``, ``./src/../src/a.py``) resolve to the SAME
  claim, so nobody walks around a lock by spelling the path differently.
* File claims are not a second lock system. They are backed by the real
  `FileLockRegistry` of `src/agent_tools/subagent_tools.py` -- the registry the
  write gate already consults before a write runs -- through the adapter in
  this file. See `FileLockRegistryBackend` for why an adapter and not the class
  itself (plan 11.2 allows exactly this).
* A handoff is a protocol, not a database update (plan 11.3): finish or pause,
  record, check that no mutating tool is running, release, acquire, emit. With
  a mutating tool still running the transfer is refused and the owner does not
  change. "Nunca cambiar el propietario escribiendo simplemente otro ID."

Reads never raise. `ready_tasks`, `blocking_open` and `snapshot` are called
while a turn is being rendered, and a ledger that throws there takes the room
down with it; they degrade to an empty or partial answer and log. Writes DO
raise `CouncilError` -- a rejected write must be visible, never swallowed.

The contracts module (`src/council/contracts.py`) owns the vocabulary and the
shapes; this module owns the rules. Payloads are filtered to the fields that
build's dataclass actually declares, so a field added or renamed there is a
dropped key and a debug line, not a dead council.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.council import contracts as _contracts
from src.council.contracts import (
    CouncilClaim,
    CouncilDecision,
    CouncilError,
    CouncilObjection,
    CouncilTask,
    has_blocking,
    new_id,
)
from src.council.contracts import supersede as _supersede

logger = logging.getLogger(__name__)

__all__ = [
    "CouncilLedger",
    "ClaimsBackend",
    "MemoryClaims",
    "FileLockRegistryBackend",
    "RoutedClaims",
    "default_claims_backend",
    "normalize_resource",
    "PATH_CLAIM_KINDS",
    "ACTIVE_CLAIM_STATES",
    "SATISFIED_TASK_STATUSES",
    "STARTABLE_TASK_STATUSES",
    "VERIFICATION_TASK_STATUSES",
    "OPEN_OBJECTION_STATUSES",
    "UNSET_PUBLISHER",
]

# --- vocabulary this module needs and the contracts do not spell out --------
#
# `contracts.py` owns the flat lists of legal values. What the ledger needs on
# top is their MEANING: which task status satisfies a dependency, which claim
# state still holds a resource, which objection severity is worth stopping for.
# Those groupings are rules, not vocabulary, so they live here.

#: Claim kinds whose resource is a filesystem path (plan 11.2).
PATH_CLAIM_KINDS = frozenset({"file", "directory"})

CLAIM_HELD = "held"
CLAIM_RELEASED = "released"
CLAIM_HANDOFF_PENDING = "handoff_pending"
CLAIM_EXPIRED = "expired"
#: States in which a claim still keeps everyone else off the resource. The list
#: is the contracts module's; only the fallback is local, for a build where the
#: constant has not been exported yet.
ACTIVE_CLAIM_STATES = frozenset(getattr(_contracts, "ACTIVE_CLAIM_STATES",
                                        ("requested", CLAIM_HELD, CLAIM_HANDOFF_PENDING)))

#: A dependency is met only by work that FINISHED WELL. A failed or cancelled
#: dependency leaves its dependents blocked -- calling them "ready" would send
#: a participant to build on something that is not there.
SATISFIED_TASK_STATUSES = frozenset({"done", "verified"})
#: Statuses from which a task can still be picked up. Both the contracts'
#: `claimed` and the plan's `assigned` are listed: they name the same moment.
#: `blocked` is NOT one of them -- somebody put it there on purpose.
STARTABLE_TASK_STATUSES = frozenset({"pending", "ready", "assigned", "claimed"})
BLOCKED_TASK_STATUS = "blocked"
#: Statuses that ASSERT the task finished well. Plan 12.1 vetoes `verified`
#: over an open blocking objection; `done` makes the same assertion about the
#: work even though it makes a weaker one about the evidence, so the veto
#: attaches to both. Everything else (`review`, `failed`, ...) stays open.
VERIFICATION_TASK_STATUSES = frozenset({"verified", "done"})
#: In order of preference, the status `assign()` moves an unstarted task to.
ASSIGNED_TASK_STATUSES = ("claimed", "assigned")

OBJECTION_OPEN = "open"
#: Statuses in which an objection is still owed an answer. `accepted` is one of
#: them: agreeing with an objection is not doing the work it asks for.
OPEN_OBJECTION_STATUSES = frozenset(getattr(_contracts, "OPEN_OBJECTION_STATUSES",
                                            (OBJECTION_OPEN, "accepted")))
SEVERITY_BLOCKING = "blocking"

DECISION_SUPERSEDED = "superseded"
DECISION_DECIDED = "decided"

#: Ring buffer of ledger events (plan 1.8). Kept for audit and for the
#: synthesis; bounded so a long session cannot grow it without limit.
MAX_EVENTS = 500


class _UnsetPublisher:
    """The value that means "nobody has said where these events go".

    A distinct object rather than `None` because `None` is a real answer -- it
    is how `service.ledger()` asks for a ledger that publishes nowhere -- and a
    single sentinel would make "not configured" and "deliberately silent" the
    same instruction. It is falsy so a careless `if publisher:` still reads as
    "unset", and it has a name so a repr in a log says which one it is."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - a debugging courtesy
        return "<unset publisher>"


UNSET_PUBLISHER = _UnsetPublisher()


def _now() -> float:
    return time.time()


def _iso(moment: Optional[float] = None) -> str:
    """An ISO-8601 UTC timestamp in the shape the contracts read back.

    Timestamps cross the contract boundary as strings, not floats: the ledger
    keeps no clock of its own and stores the same text the store persists.
    """
    if moment is None:
        return now_iso()
    return (datetime.fromtimestamp(float(moment), timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _epoch(value: Any) -> Optional[float]:
    """An ISO-8601 timestamp as epoch seconds, or None when unreadable.

    Unreadable means UNKNOWN, never "now": a lease whose deadline cannot be
    read must not expire the claim it protects (src/contracts/base.py makes the
    same rule for every timestamp Faustus stores).
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("council ledger: unreadable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# --- building contract objects without guessing their exact field set ------

def _field_names(cls: Any) -> Optional[frozenset]:
    """The fields `cls` declares, or None when they cannot be read."""
    try:
        if dataclasses.is_dataclass(cls):
            return frozenset(f.name for f in dataclasses.fields(cls))
        declared = getattr(cls, "model_fields", None) or getattr(cls, "__fields__", None)
        if isinstance(declared, dict) and declared:
            return frozenset(declared)
    except Exception:  # noqa: BLE001 - introspection is best effort
        return None
    return None


def _build(cls: Any, payload: Dict[str, Any]) -> Any:
    """Construct a contract object from the plan's field set (section 7).

    Keys the class does not declare are dropped with a debug line instead of
    raising: contracts.py and ledger.py are written against the same plan but
    not at the same moment, and a council that dies on a renamed field is worse
    than one that records the fields that exist.
    """
    data = dict(payload)
    known = _field_names(cls)
    if known is not None:
        extra = sorted(set(data) - known)
        if extra:
            logger.debug("council ledger: %s does not declare %s; dropped", getattr(cls, "__name__", cls), extra)
            data = {k: v for k, v in data.items() if k in known}
    parse = getattr(cls, "parse", None)
    if callable(parse):
        return parse(data)
    return cls(**data)


def _evolve(obj: Any, **changes: Any) -> Any:
    """A frozen contract object with `changes` applied, re-validated.

    Round-tripping through `to_dict()`/`parse()` keeps every write inside the
    contract's own validation instead of bypassing it with a raw field write.
    """
    cls = type(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _build(cls, {**to_dict(), **changes})
    if dataclasses.is_dataclass(obj):
        return dataclasses.replace(obj, **changes)
    raise CouncilError(cls.__name__, "cannot be updated: it has neither to_dict() nor dataclass fields")


def _as_dict(obj: Any) -> Dict[str, Any]:
    """A contract object as a plain dict (never raises: used by snapshot)."""
    try:
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return dict(to_dict())
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if isinstance(obj, dict):
            return dict(obj)
    except Exception:  # noqa: BLE001 - read path
        logger.debug("council ledger: could not serialise %r", type(obj), exc_info=True)
    return {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _check_vocabulary(name: str, value: str, path: str) -> None:
    """Reject a value the contracts module does not know, when it says so.

    The list lives in contracts.py; if this build does not export it the check
    is skipped rather than duplicated here -- two copies of a vocabulary drift,
    and the drift is always discovered by a user.
    """
    allowed = getattr(_contracts, name, ()) or ()
    if allowed and value not in allowed:
        raise CouncilError(path, f"unknown value {value!r}; allowed: {sorted(allowed)}")


# --- resources ------------------------------------------------------------

def normalize_resource(kind: str, resource: Any, *, workspace: str = "") -> str:
    """The canonical key for a claimed resource.

    Path kinds go through ``realpath -> normpath -> normcase`` so that
    ``src/a.py``, ``src\\a.py`` and ``./src/../src/a.py`` are ONE resource: a
    lock a second spelling walks around is not a lock (plan 20, "rutas
    equivalentes/relativas resuelven al mismo claim"). Non-path kinds -- an
    artifact id, a document id, an external URL, an effect name -- are opaque
    identifiers and are only stripped; running realpath over them would invent
    a filesystem meaning they do not have.
    """
    text = _text(resource)
    if not text or kind not in PATH_CLAIM_KINDS:
        return text
    path = text
    try:
        if workspace and not os.path.isabs(path):
            path = os.path.join(workspace, path)
        return os.path.normcase(os.path.normpath(os.path.realpath(path)))
    except (OSError, ValueError):
        # An unresolvable path is still a claimable name; normalise what we can.
        logger.debug("council ledger: realpath failed for %r", text, exc_info=True)
        return os.path.normcase(os.path.normpath(path))


def _claim_key(kind: str, normalized: str) -> str:
    """The key a claims backend is addressed by: kind first, so a file named
    `x` and an artifact named `x` are two resources, not one."""
    return f"{kind}:{normalized}"


# --- claim backends -------------------------------------------------------

class ClaimsBackend:
    """The minimal exclusion interface the ledger needs from a lock table.

    Three methods, and no lifecycle: the ledger owns the lifecycle. Anything
    that can answer them can hold council claims -- which is what lets file
    claims live in the process-wide `FileLockRegistry` while an artifact claim
    lives in a table this session owns.
    """

    def acquire(self, resource: str, holder: str) -> bool:
        raise NotImplementedError

    def release(self, resource: str, holder: str) -> bool:
        raise NotImplementedError

    def holder(self, resource: str) -> str:
        raise NotImplementedError


class MemoryClaims(ClaimsBackend):
    """An in-process exclusion table, used for the non-path claim kinds and as
    the fallback when the real registry cannot be imported."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: Dict[str, str] = {}

    def acquire(self, resource: str, holder: str) -> bool:
        with self._lock:
            current = self._owner.get(resource)
            if current is None:
                self._owner[resource] = holder
                return True
            return current == holder

    def release(self, resource: str, holder: str) -> bool:
        with self._lock:
            if self._owner.get(resource) != holder:
                return False
            self._owner.pop(resource, None)
            return True

    def holder(self, resource: str) -> str:
        with self._lock:
            return self._owner.get(resource, "")


class FileLockRegistryBackend(ClaimsBackend):
    """Path claims backed by the REAL `FileLockRegistry`.

    Why the council must not own a second file-lock table: the write gate
    already asks one question before every write
    (`subagent_tools.write_block_reason` -> `FileLockRegistry.blocked_by`). A
    parallel table would answer a different question, and the file would be
    written by whoever asked the table that said yes.

    Why an adapter rather than the class itself (plan 11.2 sanctions exactly
    this):

    * `FileLockRegistry.release(worker)` returns EVERY path that worker owns.
      That matches a delegation, where a worker's ownership ends all at once
      when the worker finishes. A council handoff moves ONE resource while its
      holder keeps the rest, and there is no per-path release; this adapter
      performs it on the registry's own `owner`/`display`/`provisional` maps,
      in one place, instead of inventing a second owner table.
    * There is no "who owns this path" accessor. `blocked_by(worker, paths)`
      answers it relative to a worker, so the adapter asks with a probe key
      that is never a holder.
    * Its instance lifetime is one delegation run, reached through a
      contextvar guard. The council owns its own instance whose lifetime is the
      SESSION (plan 11.2: "un adaptador cuyo ciclo de vida sea el de la
      actividad Consejo") and exposes it as `.registry`, so the orchestrator
      can install the same `_LockGuard` the write gate consults and a council
      driver and a delegated worker meet in one registry.

    Normalisation, first-writer-wins and conflict recording stay the
    registry's, unchanged. There is one lock semantics, not two.
    """

    #: A worker key no participant can ever have; used only to ask "is this
    #: path owned by ANYONE", which `blocked_by` answers relative to a worker.
    PROBE = "__council_holder_probe__"

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def acquire(self, resource: str, holder: str) -> bool:
        # claim() returns the paths already owned by someone ELSE; an empty
        # list means the path is now ours (or already was).
        return not self.registry.claim(holder, [resource])

    def holder(self, resource: str) -> str:
        return _text(self.registry.blocked_by(self.PROBE, [resource]))

    def release(self, resource: str, holder: str) -> bool:
        key = self.registry.norm(resource)
        if not key or self.registry.owner.get(key) != holder:
            return False
        self.registry.owner.pop(key, None)
        self.registry.display.pop(key, None)
        try:
            self.registry.provisional.discard(key)
        except AttributeError:  # older registry without the provisional set
            pass
        return True


class RoutedClaims(ClaimsBackend):
    """Sends each claim key to the backend that understands its kind.

    Keys arrive as ``kind:normalized-resource``. Path kinds are handed to the
    file backend as the bare path -- the `FileLockRegistry` must see the same
    string a write tool will see, or the two would lock different things.
    Everything else keeps the prefix, so an artifact `x` and a document `x` are
    distinct rows.
    """

    def __init__(self, files: ClaimsBackend, others: Optional[ClaimsBackend] = None) -> None:
        self.files = files
        self.others = others if others is not None else MemoryClaims()

    def _route(self, key: str) -> Tuple[ClaimsBackend, str]:
        kind, _, resource = key.partition(":")
        if kind in PATH_CLAIM_KINDS and resource:
            return self.files, resource
        return self.others, key

    def acquire(self, resource: str, holder: str) -> bool:
        backend, target = self._route(resource)
        return backend.acquire(target, holder)

    def release(self, resource: str, holder: str) -> bool:
        backend, target = self._route(resource)
        return backend.release(target, holder)

    def holder(self, resource: str) -> str:
        backend, target = self._route(resource)
        return backend.holder(target)


def default_claims_backend(workspace: str = "") -> RoutedClaims:
    """The backend a ledger uses when the caller does not supply one: the real
    `FileLockRegistry` for paths, an in-process table for everything else.

    The import is deliberately guarded. `subagent_tools` is a large module and
    a council that cannot start because the delegation stack failed to import
    would be a worse outcome than a council whose file claims are only
    process-local for that run -- but the fallback is logged loudly, because it
    IS a downgrade in safety.
    """
    files: ClaimsBackend
    try:
        from src.agent_tools.subagent_tools import FileLockRegistry

        files = FileLockRegistryBackend(FileLockRegistry(workspace or None))
    except Exception as exc:  # noqa: BLE001 - the fallback must not be fatal
        logger.warning(
            "council ledger: FileLockRegistry unavailable (%s); file claims fall back to an "
            "in-process table and will NOT be seen by the write gate", exc,
        )
        files = MemoryClaims()
    return RoutedClaims(files, MemoryClaims())


# --- the ledger -----------------------------------------------------------

class CouncilLedger:
    """The agenda, tasks, claims, objections and decisions of one session.

    State lives in memory and is written THROUGH to the store, which is the
    durable projection. It is not read back on every question: `ready_tasks()`
    and `blocking_open()` are asked while a turn is being rendered and must not
    do I/O. On construction the ledger hydrates from the store, so a recovered
    session answers the same questions it answered before the restart.

    A store write that fails is logged and counted in
    ``snapshot()["persistence_errors"]`` instead of being silently dropped: the
    session can continue on its in-memory truth, but "the decision was recorded"
    must not be a claim nobody can check.
    """

    def __init__(self, session_id: str, *, store: Any = None, claims_backend: Any = None,
                 workspace: str = "", publisher: Any = UNSET_PUBLISHER) -> None:
        self.session_id = _text(session_id)
        if not self.session_id:
            raise CouncilError("session_id", "a ledger belongs to exactly one session; got an empty id")
        self.workspace = _text(workspace)
        self._store = store if store is not None else _default_store()
        self._claims_backend = claims_backend if claims_backend is not None else default_claims_backend(self.workspace)
        self._lock = threading.RLock()
        self._tasks: Dict[str, Any] = {}
        self._claims: Dict[str, Any] = {}
        self._objections: Dict[str, Any] = {}
        self._decisions: Dict[str, Any] = {}
        #: Ids the ledger did not create but may be objected to (room messages,
        #: changesets). Registered by the orchestrator; see `track_target`.
        self._external_targets: Dict[str, str] = {}
        self._persistence_errors = 0
        self.events: List[Dict[str, Any]] = []
        #: Where `_emit` also sends what it appends.  `UNSET_PUBLISHER` means
        #: "not chosen, resolve the session's own stream on first use"; `None`
        #: means a caller explicitly asked for a silent ledger.  Those are two
        #: different instructions and one sentinel would confuse them.
        self._publisher: Any = publisher
        self._publisher_failures = 0
        self._hydrate()

    # -- who hears about it -----------------------------------------------

    def publish_to(self, publisher: Any) -> None:
        """Send this ledger's events to `publisher` as well as to `self.events`.

        The orchestrator calls this with the stream it was given, which is how a
        room built around an injected (or doubled) event stream keeps ONE stream
        rather than two: without it the ledger would resolve
        `events.stream_for(session_id)` and publish claims, objections and
        decisions into a stream nobody in that room is reading.

        `None` is a legitimate argument and means "publish nowhere": a ledger
        used for a read view (`service.ledger()`) has no page behind it, and
        re-announcing rows it merely hydrated would put events on the stream for
        transitions that happened hours ago.
        """
        self._publisher = publisher

    def _stream(self) -> Any:
        """The publisher, resolved once.  Never raises; a room whose stream
        cannot be built keeps its ledger."""
        if self._publisher is not UNSET_PUBLISHER:
            return self._publisher
        try:
            from src.council.events import stream_for

            self._publisher = stream_for(self.session_id)
        except Exception as exc:  # noqa: BLE001 - the page is not the ledger
            logger.warning("council ledger %s: no event stream in this build (%s); "
                           "ledger events stay in `events` and reach no page",
                           self.session_id, exc)
            self._publisher = None
        return self._publisher

    # -- persistence ------------------------------------------------------

    def _hydrate(self) -> None:
        """Load what the store already holds for this session (best effort)."""
        for kind, index in (("task", self._tasks), ("claim", self._claims),
                            ("objection", self._objections), ("decision", self._decisions)):
            for obj in self._store_list(kind):
                ident = _text(getattr(obj, "id", "") or _as_dict(obj).get("id"))
                if ident:
                    index[ident] = obj
        for claim in self._claims.values():
            key = self._key_of(claim)
            if key and _text(getattr(claim, "state", "")) in ACTIVE_CLAIM_STATES:
                # Re-assert the hold: after a restart the lock table is empty
                # while the ledger says the resource is taken (plan 20:
                # "reinicio durante un efecto marca incertidumbre").
                self._claims_backend.acquire(key, _text(getattr(claim, "holder_id", "")))

    def _store_list(self, kind: str) -> Sequence[Any]:
        if self._store is None:
            return ()
        fn = getattr(self._store, f"list_{kind}s", None)
        if not callable(fn):
            return ()
        try:
            return tuple(fn(self.session_id) or ())
        except TypeError:
            try:
                return tuple(fn(session_id=self.session_id) or ())
            except Exception:  # noqa: BLE001 - hydration is best effort
                logger.exception("council ledger: list_%ss failed for %s", kind, self.session_id)
        except Exception:  # noqa: BLE001
            logger.exception("council ledger: list_%ss failed for %s", kind, self.session_id)
        return ()

    def _persist(self, verb: str, kind: str, obj: Any) -> None:
        """Write a NEW record through to the store (`create_<kind>(obj)`)."""
        if self._store is None:
            return
        fn = getattr(self._store, f"{verb}_{kind}", None)
        if not callable(fn):
            return
        try:
            fn(obj)
        except Exception:  # noqa: BLE001 - the session keeps its own truth
            self._persistence_errors += 1
            logger.exception("council ledger: %s_%s failed for session %s", verb, kind, self.session_id)

    def _persist_patch(self, kind: str, ident: str, patch: Dict[str, Any]) -> None:
        """Write a CHANGE through as `update_<kind>(id, patch)`.

        The patch carries only the fields that changed, because the store
        refuses to merge a field it considers immutable -- a claim's `resource`,
        an objection's `claim`, a task's `created_at`. Sending the whole record
        back would turn every update into a rejected write.
        """
        if self._store is None or not patch:
            return
        fn = getattr(self._store, f"update_{kind}", None)
        if not callable(fn):
            return
        try:
            fn(ident, dict(patch))
        except Exception:  # noqa: BLE001 - the session keeps its own truth
            self._persistence_errors += 1
            logger.exception("council ledger: update_%s failed for %s in session %s",
                             kind, ident, self.session_id)

    # -- events -----------------------------------------------------------

    def _emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        """Append one plan-1.8 event AND put it on the room's stream.

        Emitted AFTER the state it describes was written, never before: an event
        for a transition that did not happen is how a mirror ends up ahead of the
        truth.

        The two destinations are not redundant.  `self.events` is this ledger's
        own bounded audit tail, read by `snapshot()` and by whoever is holding
        the object; the stream is what a page, a hook and the state mirror are
        subscribed to.  Appending to the first without publishing to the second
        is how a claim, an objection and a decision can all be recorded
        perfectly and be invisible to every consumer in the product — which is
        exactly what this ledger did until this line existed.

        Publishing never raises and never counts against the transition: the
        state is already durable, and a full or closed stream is the page's
        problem, not the room's.
        """
        event = {"type": event_type, "council_id": self.session_id, "at": _now()}
        event.update(payload)
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]
        stream = self._stream()
        publish = getattr(stream, "publish", None) if stream is not None else None
        if callable(publish):
            try:
                publish(event_type, **{k: v for k, v in event.items() if k != "type"})
            except Exception as exc:  # noqa: BLE001 - the page is not the room
                self._publisher_failures += 1
                logger.warning("council ledger %s: publishing %s failed: %s",
                               self.session_id, event_type, exc)
        return event

    # -- agenda and tasks -------------------------------------------------

    def add_task(self, **fields: Any) -> Any:
        """Record a task (plan 7.4). Unknown status values are refused."""
        payload: Dict[str, Any] = {
            "id": _text(fields.pop("id", "")) or new_id("task"),
            "session_id": self.session_id,
            "title": "",
            "instruction": "",
            "owner_participant_id": "",
            "reviewer_participant_id": "",
            "status": "pending",
            "depends_on": [],
            "claimed_resources": [],
            "acceptance": [],
            "run_id": "",
            "proof_id": "",
            "created_at": _iso(),
            "updated_at": _iso(),
        }
        payload.update(fields)
        payload["session_id"] = self.session_id
        _check_vocabulary("TASK_STATUSES", _text(payload["status"]), "task.status")
        task = _build(CouncilTask, payload)
        with self._lock:
            if payload["id"] in self._tasks:
                raise CouncilError("task.id", f"{payload['id']!r} already exists in this ledger")
            self._tasks[payload["id"]] = task
        self._persist("create", "task", task)
        self._emit("council_task_added", task_id=payload["id"], status=_text(payload["status"]))
        return task

    def assign(self, task_id: str, participant_id: str, *, actor: str) -> Any:
        """Give a task an owner. The status moves to `claimed` (the plan calls
        it `assigned`) only from a status that had not started; a task already
        running or in review keeps its own."""
        with self._lock:
            task = self._require_task(task_id)
            holder = _text(participant_id)
            if not holder:
                raise CouncilError("participant_id", f"task {task_id!r} cannot be assigned to nobody")
            changes: Dict[str, Any] = {"owner_participant_id": holder, "updated_at": _iso()}
            status = _text(getattr(task, "status", ""))
            if status in ("pending", "ready"):
                known = getattr(_contracts, "TASK_STATUSES", ()) or ASSIGNED_TASK_STATUSES
                assigned = next((s for s in ASSIGNED_TASK_STATUSES if s in known), "")
                if assigned:
                    changes["status"] = assigned
            updated = _evolve(task, **changes)
            self._tasks[task_id] = updated
        self._persist_patch("task", task_id, {k: v for k, v in changes.items() if k != "updated_at"})
        self._emit("council_task_assigned", task_id=task_id, participant_id=holder, actor=_text(actor))
        return updated

    def set_task_status(self, task_id: str, status: str, *, actor: str, note: str = "") -> Any:
        """Move a task to `status`.

        The statuses that assert the work finished well (`done`, and `verified`
        where the vocabulary has it) are the ones the ledger refuses on its own
        authority: an open blocking objection against the task means the room
        has an unanswered "this is wrong", and recording it as finished over
        that is the exact lie plan 12.1 forbids. Every other status stays
        reachable, so the task can still move to `review` or `failed`.
        """
        wanted = _text(status)
        _check_vocabulary("TASK_STATUSES", wanted, "task.status")
        with self._lock:
            task = self._require_task(task_id)
            if wanted in VERIFICATION_TASK_STATUSES:
                ok, reason = self.can_verify(task_id)
                if not ok:
                    raise CouncilError("task.status",
                                       f"{task_id} cannot be recorded as {wanted!r}: {reason}")
            updated = _evolve(task, status=wanted, updated_at=_iso())
            self._tasks[task_id] = updated
        self._persist_patch("task", task_id, {"status": wanted})
        self._emit("council_task_status", task_id=task_id, status=wanted,
                   actor=_text(actor), note=_text(note))
        return updated

    def _require_task(self, task_id: str) -> Any:
        task = self._tasks.get(_text(task_id))
        if task is None:
            raise CouncilError("task_id", f"no task {task_id!r} in session {self.session_id}")
        return task

    def _dependency_gap(self, task: Any) -> List[str]:
        """Which of a task's dependencies are missing or not finished well."""
        gap: List[str] = []
        for dep in getattr(task, "depends_on", ()) or ():
            dep_id = _text(dep)
            if not dep_id:
                continue
            other = self._tasks.get(dep_id)
            if other is None:
                gap.append(f"{dep_id} (unknown task)")
            elif _text(getattr(other, "status", "")) not in SATISFIED_TASK_STATUSES:
                gap.append(f"{dep_id} ({_text(getattr(other, 'status', '')) or 'no status'})")
        return gap

    def ready_tasks(self) -> List[Any]:
        """Tasks that can be picked up now: not started, not finished, not
        marked blocked, and every dependency finished well."""
        try:
            with self._lock:
                return [t for t in self._tasks.values()
                        if _text(getattr(t, "status", "")) in STARTABLE_TASK_STATUSES
                        and not self._dependency_gap(t)]
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: ready_tasks failed for %s", self.session_id)
            return []

    def blocked_tasks(self) -> List[Any]:
        """Tasks that cannot be picked up: held back by a dependency, or put in
        `blocked` by somebody who knew why. Disjoint from `ready_tasks`."""
        try:
            with self._lock:
                return [t for t in self._tasks.values()
                        if _text(getattr(t, "status", "")) == BLOCKED_TASK_STATUS
                        or (_text(getattr(t, "status", "")) in STARTABLE_TASK_STATUSES
                            and self._dependency_gap(t))]
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: blocked_tasks failed for %s", self.session_id)
            return []

    def tasks(self) -> List[Any]:
        with self._lock:
            return list(self._tasks.values())

    # -- claims -----------------------------------------------------------

    def _key_of(self, claim: Any) -> str:
        kind = _text(getattr(claim, "kind", ""))
        return _claim_key(kind, normalize_resource(kind, getattr(claim, "resource", ""),
                                                   workspace=self.workspace))

    def _active_claim_for(self, key: str) -> Optional[Any]:
        for claim in self._claims.values():
            if _text(getattr(claim, "state", "")) in ACTIVE_CLAIM_STATES and self._key_of(claim) == key:
                return claim
        return None

    def request_claim(self, *, kind: str, resource: Any, holder_id: str, task_id: str = "",
                      ttl_seconds: float = 0.0) -> Tuple[bool, Any]:
        """Ask for exclusive use of a resource.

        Returns ``(True, claim)`` when the caller now holds it -- including the
        case where it already did, so a retried turn does not create a second
        claim -- and ``(False, existing_claim)`` when somebody else does. The
        loser is told WHO holds it; a bare False sends a model into a retry loop
        against a resource that will never be free by waiting.
        """
        kind_ = _text(kind)
        holder = _text(holder_id)
        _check_vocabulary("CLAIM_KINDS", kind_, "claim.kind")
        if not kind_:
            raise CouncilError("claim.kind", "a claim needs a kind")
        if not holder:
            raise CouncilError("claim.holder_id", "a claim needs a holder")
        normalized = normalize_resource(kind_, resource, workspace=self.workspace)
        if not normalized:
            raise CouncilError("claim.resource", "a claim needs a resource")
        key = _claim_key(kind_, normalized)
        with self._lock:
            existing = self._active_claim_for(key)
            if existing is not None:
                if _text(getattr(existing, "holder_id", "")) == holder:
                    return True, existing
                self._emit("council_claim_conflicted", claim_id=_text(getattr(existing, "id", "")),
                           kind=kind_, resource=normalized, holder=_text(getattr(existing, "holder_id", "")),
                           requested_by=holder)
                return False, existing
            acquired = self._claims_backend.acquire(key, holder)
            outside = "" if acquired else (self._claims_backend.holder(key) or "unknown")
            claim = _build(CouncilClaim, {
                "id": new_id("claim"),
                "session_id": self.session_id,
                "kind": kind_,
                "resource": normalized,
                "holder_id": holder if acquired else outside,
                "task_id": _text(task_id),
                "state": CLAIM_HELD,
                "acquired_at": _iso(),
                "expires_at": _iso(_now() + float(ttl_seconds)) if ttl_seconds and ttl_seconds > 0 else "",
                "note": "" if acquired else "held outside this ledger (delegation or earlier session)",
            })
            claim_id = _text(getattr(claim, "id", "")) or new_id("claim")
            self._claims[claim_id] = claim
            if acquired and task_id:
                self._attach_resource(_text(task_id), normalized)
        self._persist("create", "claim", claim)
        if not acquired:
            logger.warning("council ledger: %s is already held by %r outside this ledger; %s refused",
                           normalized, outside, holder)
            self._emit("council_claim_conflicted", claim_id=claim_id, kind=kind_,
                       resource=normalized, holder=outside, requested_by=holder)
            return False, claim
        self._emit("council_claim_acquired", claim_id=claim_id, kind=kind_,
                   resource=normalized, holder=holder, task_id=_text(task_id),
                   spelled=_text(resource))
        return True, claim

    def _attach_resource(self, task_id: str, normalized: str) -> None:
        """Record on the task which resources its work reserved (plan 7.4), so
        the closing summary lists changes from the ledger and not from prose."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        current = [_text(r) for r in (getattr(task, "claimed_resources", ()) or ())]
        if normalized in current:
            return
        resources = current + [normalized]
        updated = _evolve(task, claimed_resources=resources, updated_at=_iso())
        self._tasks[task_id] = updated
        self._persist_patch("task", task_id, {"claimed_resources": resources})

    def release_claim(self, claim_id: str, *, actor: str) -> bool:
        """Give a resource back. False when there was nothing to give back."""
        with self._lock:
            claim = self._claims.get(_text(claim_id))
            if claim is None or _text(getattr(claim, "state", "")) not in ACTIVE_CLAIM_STATES:
                return False
            released = self._release_locked(claim, CLAIM_RELEASED)
        if released is None:
            return False
        self._persist_patch("claim", _text(claim_id), {"state": CLAIM_RELEASED})
        self._emit("council_claim_released", claim_id=_text(claim_id), actor=_text(actor),
                   kind=_text(getattr(released, "kind", "")),
                   resource=_text(getattr(released, "resource", "")),
                   holder=_text(getattr(released, "holder_id", "")))
        return True

    def _release_locked(self, claim: Any, state: str) -> Optional[Any]:
        """Drop the backend hold and move the claim to a final state. Called
        with the ledger lock held."""
        claim_id = _text(getattr(claim, "id", ""))
        holder = _text(getattr(claim, "holder_id", ""))
        key = self._key_of(claim)
        try:
            self._claims_backend.release(key, holder)
        except Exception:  # noqa: BLE001 - the ledger still records the release
            logger.exception("council ledger: backend release failed for %s", key)
        updated = _evolve(claim, state=state)
        self._claims[claim_id] = updated
        return updated

    def claims_of(self, holder_id: str) -> List[Any]:
        """Every resource this holder currently keeps others off."""
        holder = _text(holder_id)
        try:
            with self._lock:
                return [c for c in self._claims.values()
                        if _text(getattr(c, "holder_id", "")) == holder
                        and _text(getattr(c, "state", "")) in ACTIVE_CLAIM_STATES]
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: claims_of failed for %s", holder)
            return []

    def holder_of(self, kind: str, resource: Any) -> Optional[str]:
        """Who holds this resource, under any spelling of it, or None."""
        try:
            kind_ = _text(kind)
            key = _claim_key(kind_, normalize_resource(kind_, resource, workspace=self.workspace))
            with self._lock:
                claim = self._active_claim_for(key)
            if claim is not None:
                return _text(getattr(claim, "holder_id", "")) or None
            outside = _text(self._claims_backend.holder(key))
            return outside or None
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: holder_of failed for %s/%r", kind, resource)
            return None

    def expire_claims(self, *, now: Optional[float] = None) -> int:
        """Release claims whose lease ran out. Returns how many were expired.

        A lease exists so a crashed participant does not fence a file off for
        the rest of the session; it is not a substitute for releasing.
        """
        moment = _now() if now is None else float(now)
        expired: List[Any] = []
        with self._lock:
            for claim in list(self._claims.values()):
                if _text(getattr(claim, "state", "")) not in ACTIVE_CLAIM_STATES:
                    continue
                deadline = _epoch(getattr(claim, "expires_at", ""))
                if deadline is None or deadline > moment:
                    continue
                updated = self._release_locked(claim, CLAIM_EXPIRED)
                if updated is not None:
                    expired.append(updated)
        for claim in expired:
            self._persist_patch("claim", _text(getattr(claim, "id", "")), {"state": CLAIM_EXPIRED})
            self._emit("council_claim_released", claim_id=_text(getattr(claim, "id", "")),
                       reason="expired", resource=_text(getattr(claim, "resource", "")),
                       holder=_text(getattr(claim, "holder_id", "")))
        return len(expired)

    def handoff(self, claim_id: str, *, to: str, actor: str,
                mutating_active: bool = False) -> Dict[str, Any]:
        """Transfer one claim, following the protocol of plan 11.3.

        finish or pause -> record -> check no mutating tool is running ->
        release -> acquire -> emit. `mutating_active=True` is the caller
        saying "a write/patch/command from the current owner is still in
        flight"; the transfer is then REFUSED and the owner does not change.
        Handing a half-written file to a second writer is how a repository ends
        up in a state neither participant intended, and no later apology
        reconstructs it.

        Never raises: a refused handoff is an answer the orchestrator acts on,
        not an exception it has to catch mid-turn.
        """
        target = _text(to)
        cid = _text(claim_id)
        with self._lock:
            claim = self._claims.get(cid)
            if claim is None:
                return {"ok": False, "reason": f"no claim {cid!r} in session {self.session_id}",
                        "claim_id": cid, "to": target}
            holder = _text(getattr(claim, "holder_id", ""))
            state = _text(getattr(claim, "state", ""))
            resource = _text(getattr(claim, "resource", ""))
            kind = _text(getattr(claim, "kind", ""))
            base = {"claim_id": cid, "kind": kind, "resource": resource,
                    "from": holder, "to": target, "actor": _text(actor)}
            if state not in ACTIVE_CLAIM_STATES:
                return {**base, "ok": False,
                        "reason": f"claim {cid} is {state or 'in no state'}; only a held claim can be handed off"}
            if not target:
                return {**base, "ok": False, "reason": "a handoff needs a new holder"}
            if target == holder:
                return {**base, "ok": True, "changed": False,
                        "reason": f"{target} already holds {resource}"}
            # Step 2: the intent is recorded before anything moves, so a crash
            # between the gate and the acquire is visible as handoff_pending
            # rather than as a resource with two plausible owners.
            pending = _evolve(claim, state=CLAIM_HANDOFF_PENDING)
            self._claims[cid] = pending
            # Step 3: the gate.
            if mutating_active:
                self._claims[cid] = _evolve(pending, state=CLAIM_HELD)
                reason = (f"a mutating tool of {holder} is still running on {resource}; "
                          f"finish or pause it before handing the claim to {target}")
                self._emit("council_claim_handoff_refused", **base, reason=reason)
                logger.info("council ledger: handoff of %s refused - %s", cid, reason)
                return {**base, "ok": False, "changed": False, "reason": reason}
            key = self._key_of(pending)
            self._claims_backend.release(key, holder)
            if not self._claims_backend.acquire(key, target):
                # Put it back where it was: a resource with no owner is worse
                # than one with the wrong owner, because nothing refuses a write.
                self._claims_backend.acquire(key, holder)
                self._claims[cid] = _evolve(pending, state=CLAIM_HELD)
                blocker = self._claims_backend.holder(key)
                reason = f"{target} could not acquire {resource}; it is held by {blocker or 'someone else'}"
                self._emit("council_claim_handoff_refused", **base, reason=reason)
                return {**base, "ok": False, "changed": False, "reason": reason}
            moment = _iso()
            updated = _evolve(pending, holder_id=target, state=CLAIM_HELD, acquired_at=moment)
            self._claims[cid] = updated
        self._persist_patch("claim", cid, {"holder_id": target, "state": CLAIM_HELD,
                                           "acquired_at": moment})
        event = self._emit("council_claim_transferred", **base)
        return {**base, "ok": True, "changed": True, "state": CLAIM_HELD, "event": event,
                "reason": f"{resource} moved from {holder} to {target}"}

    def release_on_cancel(self, holder_id: str, *,
                          mutating_active_resources: Sequence[Any] = ()) -> Dict[str, Any]:
        """Release what a cancelled participant held, EXCEPT what is still in use.

        Cancelling stops new work; it does not stop a write already inside the
        filesystem. Releasing such a resource would let a second writer in
        while the first one is still finishing, so those claims are kept and
        reported (plan 20: "cancelar no libera prematuramente un recurso en
        uso"). The caller is told exactly what was left behind and why.
        """
        holder = _text(holder_id)
        busy = [r for r in (mutating_active_resources or ()) if _text(r)]
        released: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        with self._lock:
            for claim in list(self._claims.values()):
                if _text(getattr(claim, "holder_id", "")) != holder:
                    continue
                if _text(getattr(claim, "state", "")) not in ACTIVE_CLAIM_STATES:
                    continue
                kind = _text(getattr(claim, "kind", ""))
                resource = _text(getattr(claim, "resource", ""))
                cid = _text(getattr(claim, "id", ""))
                entry = {"claim_id": cid, "kind": kind, "resource": resource}
                if any(normalize_resource(kind, r, workspace=self.workspace) == resource for r in busy):
                    kept.append({**entry, "reason": "a mutating tool is still running on it"})
                    continue
                updated = self._release_locked(claim, CLAIM_RELEASED)
                if updated is not None:
                    released.append(entry)
        for entry in released:
            self._persist_patch("claim", entry["claim_id"], {"state": CLAIM_RELEASED})
            self._emit("council_claim_released", reason="cancelled", holder=holder, **entry)
        if kept:
            logger.info("council ledger: cancel of %s kept %d resource(s) in use", holder, len(kept))
        return {"holder_id": holder, "released": released, "kept": kept}

    # -- objections -------------------------------------------------------

    def track_target(self, target_id: str, kind: str = "message") -> None:
        """Register an id the ledger did not create but may be objected to --
        a room message, a changeset. Without this an objection against a
        message could only be checked when a store is attached, and an
        objection nobody can attach to anything is a complaint, not a record.
        """
        ident = _text(target_id)
        if ident:
            self._external_targets[ident] = _text(kind) or "message"

    def _target_exists(self, target_id: str) -> bool:
        ident = _text(target_id)
        if not ident:
            return False
        if (ident in self._tasks or ident in self._decisions or ident in self._objections
                or ident in self._claims or ident in self._external_targets):
            return True
        for getter in ("get_message", "get_task", "get_decision", "get_objection", "get_claim"):
            fn = getattr(self._store, getter, None) if self._store is not None else None
            if not callable(fn):
                continue
            try:
                if fn(ident) is not None:
                    return True
            except Exception:  # noqa: BLE001 - a missing row is not an error here
                continue
        return False

    def object_to(self, **fields: Any) -> Any:
        """Record an objection (plan 12.1).

        An objection must point at something that exists. "This is wrong"
        attached to nothing cannot be resolved, cannot block a verification and
        cannot be answered -- it only makes the summary longer (plan 20: "una
        objeción referencia un objetivo existente").
        """
        payload: Dict[str, Any] = {
            "id": _text(fields.pop("id", "")) or new_id("obj"),
            "session_id": self.session_id,
            "target_kind": "message",
            "target_id": "",
            "author_id": "",
            "severity": "concern",
            "claim": "",
            "evidence_refs": [],
            "proposed_resolution": "",
            "status": OBJECTION_OPEN,
            "created_at": _iso(),
        }
        # The plan (12.1) writes `target` and `evidence`; the contract calls
        # them `target_kind` and `evidence_refs`. Accept both spellings rather
        # than dropping a caller's target on the floor.
        for spelled, stored in (("target", "target_kind"), ("evidence", "evidence_refs")):
            if spelled in fields:
                fields[stored] = fields.pop(spelled)
        payload.update(fields)
        payload["session_id"] = self.session_id
        target_id = _text(payload["target_id"])
        severity = _text(payload["severity"])
        _check_vocabulary("OBJECTION_SEVERITIES", severity, "objection.severity")
        _check_vocabulary("OBJECTION_STATUSES", _text(payload["status"]), "objection.status")
        if not _text(payload["claim"]):
            raise CouncilError("objection.claim", "an objection must say what is wrong")
        with self._lock:
            if not self._target_exists(target_id):
                raise CouncilError(
                    "objection.target_id",
                    f"{target_id or '(empty)'!r} is not a task, decision, claim, objection or "
                    f"registered message of session {self.session_id}; an objection cannot point at nothing")
            objection = _build(CouncilObjection, payload)
            self._objections[_text(payload["id"])] = objection
        self._persist("create", "objection", objection)
        self._emit("council_objection_recorded", objection_id=_text(payload["id"]),
                   target_id=target_id, severity=severity, author=_text(payload["author_id"]))
        return objection

    def resolve_objection(self, objection_id: str, status: str, *, actor: str, note: str = "") -> Any:
        """Close an objection with an explicit outcome. `open` is not one:
        a resolution that leaves it open is not a resolution."""
        wanted = _text(status)
        _check_vocabulary("OBJECTION_STATUSES", wanted, "objection.status")
        if wanted == OBJECTION_OPEN:
            raise CouncilError("objection.status", "resolving an objection needs an outcome, not 'open'")
        with self._lock:
            objection = self._objections.get(_text(objection_id))
            if objection is None:
                raise CouncilError("objection_id", f"no objection {objection_id!r} in session {self.session_id}")
            patch = {"status": wanted, "resolved_at": _iso(), "resolution_note": _text(note)}
            updated = _evolve(objection, **patch)
            self._objections[_text(objection_id)] = updated
        self._persist_patch("objection", _text(objection_id), patch)
        self._emit("council_objection_resolved", objection_id=_text(objection_id), status=wanted,
                   actor=_text(actor), note=_text(note))
        return updated

    def open_objections(self, *, target_id: str = "", severity: str = "") -> List[Any]:
        """Objections still owed an answer, optionally narrowed.

        `accepted` counts as open (the contracts say so): agreeing with an
        objection is not doing the work it asks for, and a close that treats
        the two as the same is how "we accepted that point" becomes "resolved".
        """
        want_target = _text(target_id)
        want_severity = _text(severity)
        try:
            with self._lock:
                out = []
                for objection in self._objections.values():
                    if _text(getattr(objection, "status", "")) not in OPEN_OBJECTION_STATUSES:
                        continue
                    if want_target and _text(getattr(objection, "target_id", "")) != want_target:
                        continue
                    if want_severity and _text(getattr(objection, "severity", "")) != want_severity:
                        continue
                    out.append(objection)
                return out
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: open_objections failed for %s", self.session_id)
            return []

    def blocking_open(self, *, target_id: str = "") -> bool:
        """Is there an open blocking objection (against `target_id`, if given)?

        The judgement is `contracts.has_blocking`, not a second copy of it: this
        answer gates a verification, and two implementations of "is this
        blocking" is exactly how a room ends up blocked in one view and
        finished in another.
        """
        return has_blocking(self.open_objections(target_id=target_id))

    # -- decisions --------------------------------------------------------

    def _new_decision(self, **fields: Any) -> Any:
        """Build a decision record without storing it (used by `supersede`)."""
        payload: Dict[str, Any] = {
            "id": _text(fields.pop("id", "")) or new_id("decision"),
            "session_id": self.session_id,
            "question": "",
            "status": DECISION_DECIDED,
            "chosen": "",
            "alternatives": [],
            "rationale": [],
            "supporters": [],
            "dissenters": [],
            "evidence_refs": [],
            "supersedes": "",
            "created_at": _iso(),
        }
        payload.update(fields)
        payload["session_id"] = self.session_id
        _check_vocabulary("DECISION_STATUSES", _text(payload["status"]), "decision.status")
        if not _text(payload["question"]):
            raise CouncilError("decision.question", "a decision must record the question it answers")
        return _build(CouncilDecision, payload)

    def decide(self, **fields: Any) -> Any:
        """Record a decision (plan 7.5). Dissent is stored, not smoothed over:
        `dissenters` is part of the record and reaches the closing summary."""
        decision = self._new_decision(**fields)
        ident = _text(getattr(decision, "id", ""))
        with self._lock:
            if ident in self._decisions:
                raise CouncilError("decision.id", f"{ident!r} already exists in this ledger")
            self._decisions[ident] = decision
        self._persist("create", "decision", decision)
        self._emit("council_decision_recorded", decision_id=ident,
                   question=_text(getattr(decision, "question", "")),
                   supersedes=_text(getattr(decision, "supersedes", "")),
                   dissenters=list(getattr(decision, "dissenters", ()) or ()))
        return decision

    def supersede_decision(self, decision_id: str, **fields: Any) -> Any:
        """Replace a decision with a NEW one that points back at it.

        There is deliberately no way to edit a decision in place. A room that
        can rewrite what it decided cannot be audited afterwards: the record
        would always agree with the outcome. The old decision keeps its text
        and gains one mark, `superseded`; the reasoning for the change lives in
        the new decision (plan 7.5: "nunca editar silenciosamente una decisión
        anterior: crear otra con `supersedes`").

        The rule itself is `contracts.supersede()` -- one implementation, so a
        caller that never touches this ledger gets the same answer. Only the
        QUESTION is carried over when the caller does not restate it; the
        rationale, supporters and dissenters are never inherited, because a
        superseding decision that reused the old supporters would put names
        behind a conclusion they never saw.
        """
        old_id = _text(decision_id)
        with self._lock:
            old = self._decisions.get(old_id)
            if old is None:
                raise CouncilError("decision_id", f"no decision {old_id!r} in session {self.session_id}")
            if _text(getattr(old, "status", "")) == DECISION_SUPERSEDED:
                # `contracts.supersede()` refuses this too; naming the
                # replacement here is the difference between an error a reader
                # can act on and one they have to go looking for.
                newer = [_text(getattr(d, "id", "")) for d in self._decisions.values()
                         if _text(getattr(d, "supersedes", "")) == old_id]
                raise CouncilError(
                    "decision_id",
                    f"{old_id} was already superseded by {newer or ['an earlier decision']}; "
                    f"supersede the decision that is in force")
            payload = dict(fields)
            payload.setdefault("question", _text(getattr(old, "question", "")))
            payload["supersedes"] = old_id
            retired, replacement = _supersede(old, self._new_decision(**payload))
            self._decisions[old_id] = retired
            self._decisions[_text(getattr(replacement, "id", ""))] = replacement
        self._persist_supersede(old_id, replacement)
        self._emit("council_decision_recorded", decision_id=_text(getattr(replacement, "id", "")),
                   question=_text(getattr(replacement, "question", "")), supersedes=old_id,
                   dissenters=list(getattr(replacement, "dissenters", ()) or ()))
        self._emit("council_decision_superseded", decision_id=old_id,
                   replaced_by=_text(getattr(replacement, "id", "")))
        return replacement

    def _persist_supersede(self, old_id: str, replacement: Any) -> None:
        """Retire and replace in ONE store call when the store offers it.

        `CouncilStore` has no `update_decision` on purpose, and it refuses to
        reach `superseded` any other way: a decision retired without its
        replacement written in the same breath leaves a room with a hole where
        its answer used to be.
        """
        if self._store is None:
            return
        fn = getattr(self._store, "supersede_decision", None)
        if callable(fn):
            try:
                fn(old_id, replacement)
                return
            except Exception:  # noqa: BLE001 - the session keeps its own truth
                self._persistence_errors += 1
                logger.exception("council ledger: supersede_decision failed for %s", old_id)
                return
        self._persist("create", "decision", replacement)

    def decisions(self, *, include_superseded: bool = False) -> List[Any]:
        """The decisions in force, newest last. Superseded ones on request."""
        try:
            with self._lock:
                return [d for d in self._decisions.values()
                        if include_superseded or _text(getattr(d, "status", "")) != DECISION_SUPERSEDED]
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: decisions failed for %s", self.session_id)
            return []

    # -- reading ----------------------------------------------------------

    def can_verify(self, task_id: str) -> Tuple[bool, str]:
        """May this task be declared `verified`, and if not, why not.

        An open blocking objection against the task is a veto: someone said
        this is wrong and nobody answered. It stops THIS task only -- the rest
        of the agenda keeps moving (plan 12.1: "no necesariamente impide que
        otras tareas continúen").
        """
        ident = _text(task_id)
        try:
            with self._lock:
                task = self._tasks.get(ident)
                if task is None:
                    return False, f"no task {ident!r} in session {self.session_id}"
                blockers = self.open_objections(target_id=ident, severity=SEVERITY_BLOCKING)
                if blockers:
                    names = ", ".join(
                        f"{_text(getattr(o, 'id', '')) or '?'} ({_text(getattr(o, 'claim', '')) or 'no claim text'})"
                        for o in blockers)
                    return False, f"open blocking objection: {names}"
                return True, f"no open blocking objection targets {ident}"
        except Exception:  # noqa: BLE001 - read path
            logger.exception("council ledger: can_verify failed for %s", ident)
            return False, "the ledger could not be read; see the log"

    @property
    def file_lock_registry(self) -> Any:
        """The `FileLockRegistry` behind this ledger's file claims, or None.

        Exposed so the orchestrator can install the same lock guard the write
        gate consults (`subagent_tools._LockGuard`): a council driver and a
        delegated worker must meet in ONE registry, or each will be told the
        file is free.
        """
        backend = getattr(self._claims_backend, "files", self._claims_backend)
        return getattr(backend, "registry", None)

    def snapshot(self) -> Dict[str, Any]:
        """Everything the closing summary is allowed to be built from.

        `synthesis.build()` reads this and nothing else: the summary is a view
        of the ledger, not a re-reading of the transcript (plan 12.2).
        """
        try:
            with self._lock:
                tasks = [_as_dict(t) for t in self._tasks.values()]
                claims = [_as_dict(c) for c in self._claims.values()]
                objections = [_as_dict(o) for o in self._objections.values()]
                decisions = [_as_dict(d) for d in self._decisions.values()]
                ready = [_text(getattr(t, "id", "")) for t in self.ready_tasks()]
                blocked = [_text(getattr(t, "id", "")) for t in self.blocked_tasks()]
                open_objections = [_as_dict(o) for o in self.open_objections()]
                held = [c for c in claims if _text(c.get("state", "")) in ACTIVE_CLAIM_STATES]
                return {
                    "session_id": self.session_id,
                    "tasks": tasks,
                    "claims": claims,
                    "objections": objections,
                    "decisions": decisions,
                    "ready_task_ids": ready,
                    "blocked_task_ids": blocked,
                    "open_objections": open_objections,
                    "blocking_open": any(_text(o.get("severity", "")) == SEVERITY_BLOCKING
                                         for o in open_objections),
                    "held_claims": held,
                    "events": list(self.events),
                    "persistence_errors": self._persistence_errors,
                    # How many of those events never reached the page. Counted
                    # for the same reason as `persistence_errors`: a room whose
                    # stream is broken looks exactly like a room where nothing
                    # happened, and the difference has to be visible somewhere.
                    "publisher_failures": self._publisher_failures,
                    "counts": {
                        "tasks": len(tasks),
                        "claims_held": len(held),
                        "objections_open": len(open_objections),
                        "decisions": len([d for d in decisions
                                          if _text(d.get("status", "")) != DECISION_SUPERSEDED]),
                    },
                }
        except Exception:  # noqa: BLE001 - read path: a broken snapshot must
            # still say which session it is about, or the caller cannot even
            # report the failure.
            logger.exception("council ledger: snapshot failed for %s", self.session_id)
            return {"session_id": self.session_id, "tasks": [], "claims": [], "objections": [],
                    "decisions": [], "ready_task_ids": [], "blocked_task_ids": [],
                    "open_objections": [], "blocking_open": False, "held_claims": [], "events": [],
                    "persistence_errors": self._persistence_errors,
                    "publisher_failures": self._publisher_failures,
                    "counts": {}, "unreadable": True}


def _default_store() -> Any:
    """The persistence layer, when this build has one.

    A ledger without a store is a valid, useful object -- a dry run, a test, a
    session whose durability is somebody else's problem -- so a missing
    persistence module is a debug line, not a failure to construct.
    """
    try:
        from src.council.persistence import store

        return store()
    except Exception as exc:  # noqa: BLE001 - persistence is optional here
        logger.debug("council ledger: no persistence store (%s); running in memory", exc)
        return None
