"""resource_ownership.py — a lease over any resource: a file, a browser
session, a model instance (PLAN-03 / QA-16).

`FileLockRegistry` (src/agent_tools/subagent_tools.py) already gives
delegated workers exclusive ownership of files, and it works — a write to a
file another worker owns is refused with an explanatory error. But it only
knows about files, and it has no ceiling: a worker that dies without calling
`release()` leaves its files locked for the rest of the delegation run, with
no record of who had them or for how long.

This module generalizes ownership to ANY resource kind and adds the two
things `FileLockRegistry` deliberately does not have:

* a TTL, so a lease outlives its holder by seconds, not forever — a lease
  that is not renewed is reclaimable once it expires, not a permanent fence;
* a reconciliation trail: who had a resource and, if anything was ever
  reported through `note_activity`, what they did with it, recorded at the
  moment the lease lapses.

File resources are not re-implemented here. `_key()` reuses
`FileLockRegistry.norm()` for path canonicalisation, and every acquire/
release of a `"file"` lease is mirrored into an actual `FileLockRegistry` —
supplied by the caller (the same one a delegation run already has) or a
private one otherwise — so `write_block_reason()` and the rest of the
existing file-locking machinery keep seeing the truth. This module adds
TTL and reconciliation ON TOP of that registry, not a second store for the
same fact.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent_tools.subagent_tools import FileLockRegistry

#: Not a closed enum — `_key()` treats `"file"` specially (routed through
#: `FileLockRegistry`) and everything else generically. Listed for callers
#: that want a fixed vocabulary to validate against; passing any other short
#: string as `kind` still works exactly like `"generic"`/`"browser_session"`/
#: `"model"` do.
RESOURCE_KINDS = ("file", "browser_session", "model", "generic")

#: A worker that goes silent without releasing its lease is reclaimable after
#: this many seconds. Generous on purpose — a subagent doing real extraction
#: work can go quiet for a while; this is a dead-holder backstop, not a
#: contention timer.
DEFAULT_TTL_SECONDS = 300.0


@dataclass
class Lease:
    """One resource, held by one owner, for a bounded time."""

    resource: str
    kind: str
    owner: str
    acquired_at: float
    ttl_seconds: float
    renewed_at: float = 0.0
    #: The delegation task this owner is doing right now, for a screen that
    #: shows "who edits what" (PLAN-03) rather than just an opaque owner key.
    #: Optional — a caller outside a delegation run (a model lease, a
    #: council session) has no task to name, and leaves it "".
    task_id: str = ""
    #: Free-text notes a holder chose to leave via `note_activity()` before
    #: the lease lapsed — "what it did", for the reconciliation record. Never
    #: inferred: an expiry with none of these says so honestly.
    activity: List[str] = field(default_factory=list)

    def expires_at(self) -> float:
        return (self.renewed_at or self.acquired_at) + self.ttl_seconds

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource": self.resource, "kind": self.kind, "owner": self.owner,
            "task_id": self.task_id, "acquired_at": self.acquired_at,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at(), "activity": list(self.activity),
        }


@dataclass
class ReconciliationRecord:
    """What happened to a resource when its lease ended: who had it, when,
    and — only if the holder reported it through `note_activity()` — what it
    did. A record with an empty `activity` list is not a gap in the log; it
    is the honest answer "nothing was reported"."""

    resource: str
    kind: str
    previous_owner: str
    acquired_at: float
    ended_at: float
    reason: str
    activity: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource": self.resource, "kind": self.kind,
            "previous_owner": self.previous_owner, "acquired_at": self.acquired_at,
            "ended_at": self.ended_at, "reason": self.reason,
            "activity": list(self.activity),
        }


@dataclass
class ConflictAttempt:
    """A second delegation asked for a resource a first one already holds.
    `acquire()` still refuses it (leases stay exclusive) — this is only the
    record of the attempt, for a screen that wants to show "task B is
    waiting on a resource task A owns" rather than silence."""

    resource: str
    kind: str
    holder: str
    holder_task_id: str
    requester: str
    requester_task_id: str
    at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource": self.resource, "kind": self.kind,
            "holder": self.holder, "holder_task_id": self.holder_task_id,
            "requester": self.requester, "requester_task_id": self.requester_task_id,
            "at": self.at,
        }


class ResourceOwnershipRegistry:
    """Lease-based ownership for any resource kind.

    `"file"` resources are delegated to a `FileLockRegistry` (reused, per
    PLAN-03, rather than duplicated) for path identity and the conflict
    bookkeeping the rest of the codebase already reads; every other kind is
    tracked here directly. Either way, TTL and reconciliation are uniform
    across kinds — that is the whole point of generalizing past files.
    """

    def __init__(self, *, file_registry: Optional[FileLockRegistry] = None,
                 default_ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._leases: Dict[str, Lease] = {}   # "kind:key" -> Lease
        self._history: List[ReconciliationRecord] = []
        #: Every time a second owner's `acquire()` was refused because
        #: someone else already holds the resource — a bounded ring buffer
        #: (see `acquire()`), read by `conflicts()` for the "quién edita
        #: qué" screen's conflict state (PLAN-03).
        self._conflicts: List[ConflictAttempt] = []
        self.default_ttl_seconds = default_ttl_seconds
        # The authority for "file" identity/conflicts stays FileLockRegistry;
        # this registry only adds a TTL and a reconciliation trail over it.
        self.files = file_registry if file_registry is not None else FileLockRegistry(workspace=None)

    # -- internals ---------------------------------------------------

    def _key(self, kind: str, resource: str) -> str:
        if kind == "file":
            normalized = self.files.norm(resource) or resource
            return f"file:{normalized}"
        return f"{kind}:{resource}"

    def _reap_if_expired(self, key: str, *, now: Optional[float] = None) -> None:
        lease = self._leases.get(key)
        if lease is not None and lease.expired(now):
            self._end(key, reason="lease TTL elapsed with no renewal")

    def _end(self, key: str, *, reason: str) -> None:
        lease = self._leases.pop(key, None)
        if lease is None:
            return
        if lease.kind == "file":
            self.files.release(lease.owner)
        self._history.append(ReconciliationRecord(
            resource=lease.resource, kind=lease.kind, previous_owner=lease.owner,
            acquired_at=lease.acquired_at, ended_at=time.time(), reason=reason,
            activity=list(lease.activity)))

    # -- public API ----------------------------------------------------

    def acquire(self, kind: str, resource: str, owner: str, *,
                task_id: str = "", ttl_seconds: Optional[float] = None,
                now: Optional[float] = None) -> bool:
        """Claim `resource` for `owner`, doing work as `task_id` (a
        delegation task/run id, for the "who edits what" screen — optional,
        purely descriptive, never part of identity).

        Returns `False` when a DIFFERENT owner already holds an unexpired
        lease on it — and records a `ConflictAttempt` (see `conflicts()`) so a
        screen can show that this task is waiting on that one. An expired
        lease is reaped (and reconciled) first, so a holder that died without
        releasing never blocks a new owner forever. The same owner
        re-acquiring what it already holds renews it instead of failing — a
        subagent polling its own lease should not have to special-case "I
        already have this".
        """
        key = self._key(kind, resource)
        self._reap_if_expired(key, now=now)
        moment = now if now is not None else time.time()
        current = self._leases.get(key)
        if current is not None:
            if current.owner == owner:
                current.renewed_at = moment
                return True
            self._record_conflict(current, kind, resource, owner, task_id, moment)
            return False
        if kind == "file":
            taken = self.files.claim(owner, [resource])
            if taken:
                # FileLockRegistry itself already held it (claimed outside
                # this registry) — same conflict, no Lease here to read the
                # holder's task_id from.
                self._conflicts.append(ConflictAttempt(
                    resource=resource, kind=kind, holder=self.files.owner.get(self.files.norm(resource)) or "",
                    holder_task_id="", requester=owner, requester_task_id=task_id, at=moment))
                del self._conflicts[:-200]
                return False
        self._leases[key] = Lease(resource=resource, kind=kind, owner=owner,
                                  acquired_at=moment, task_id=task_id,
                                  ttl_seconds=ttl_seconds if ttl_seconds is not None
                                  else self.default_ttl_seconds)
        return True

    def _record_conflict(self, held_by: "Lease", kind: str, resource: str,
                          requester: str, requester_task_id: str, moment: float) -> None:
        self._conflicts.append(ConflictAttempt(
            resource=resource, kind=kind, holder=held_by.owner,
            holder_task_id=held_by.task_id, requester=requester,
            requester_task_id=requester_task_id, at=moment))
        # A ring buffer, not an ever-growing log — this is UI-conflict state,
        # not the reconciliation trail (`history()`), which stays complete.
        del self._conflicts[:-200]

    def renew(self, kind: str, resource: str, owner: str, *, now: Optional[float] = None) -> bool:
        """Push a held lease's expiry out from now. `False` if `owner` does
        not currently hold it (including: it already expired)."""
        key = self._key(kind, resource)
        self._reap_if_expired(key, now=now)
        lease = self._leases.get(key)
        if lease is None or lease.owner != owner:
            return False
        lease.renewed_at = now if now is not None else time.time()
        return True

    def note_activity(self, kind: str, resource: str, owner: str, activity: str) -> bool:
        """Record what `owner` did with a resource it still holds, so that IF
        the lease later lapses without an explicit `release()`, the
        reconciliation record says more than just a name and a timestamp.
        `False` if `owner` does not hold this lease right now."""
        key = self._key(kind, resource)
        lease = self._leases.get(key)
        if lease is None or lease.owner != owner:
            return False
        text = str(activity or "").strip()
        if text:
            lease.activity.append(text)
        return True

    def release(self, kind: str, resource: str, owner: str, *, note: str = "") -> bool:
        """Give up a resource `owner` still holds — ordinary completion, not
        an expiry. Always reconciled (like an expiry) so the history is a
        complete account of every lease that ever ended, not just the ones
        that lapsed; `note` (or the lease's own `note_activity` trail) becomes
        the record's activity if given. `False` if `owner` does not hold it.
        """
        key = self._key(kind, resource)
        lease = self._leases.get(key)
        if lease is None or lease.owner != owner:
            return False
        text = str(note or "").strip()
        if text:
            lease.activity.append(text)
        self._end(key, reason="released")
        return True

    def owner_of(self, kind: str, resource: str, *, now: Optional[float] = None) -> Optional[str]:
        key = self._key(kind, resource)
        self._reap_if_expired(key, now=now)
        lease = self._leases.get(key)
        return lease.owner if lease else None

    def sweep_expired(self, *, now: Optional[float] = None) -> List[ReconciliationRecord]:
        """Reap every lapsed lease now (rather than lazily, on the next call
        that happens to touch it) and return the reconciliation records this
        call produced — for a periodic housekeeping tick, or the start of a
        new delegation run that wants to know what just freed up."""
        moment = now if now is not None else time.time()
        before = len(self._history)
        for key in [k for k, lease in self._leases.items() if lease.expired(moment)]:
            self._end(key, reason="lease TTL elapsed with no renewal")
        return self._history[before:]

    def history(self) -> List[ReconciliationRecord]:
        """Every lease that has ended, oldest first — released and expired
        alike (see `release()`)."""
        return list(self._history)

    def active_leases(self) -> List[Lease]:
        return list(self._leases.values())

    def conflicts(self, *, since: Optional[float] = None) -> List[ConflictAttempt]:
        """Refused acquisition attempts, oldest first. `since` (a timestamp)
        limits this to conflicts that happened at or after it — a screen
        polling this registry wants "what's new", not the whole ring buffer
        every time."""
        if since is None:
            return list(self._conflicts)
        return [c for c in self._conflicts if c.at >= since]


# ---------------------------------------------------------------------------
# Process-wide registry
# ---------------------------------------------------------------------------
#
# One registry shared by every delegation run in this process, so a "who
# edits what" screen (PLAN-03) has one place to read leases and conflicts
# from instead of reaching into each run's private FileLockRegistry. A
# delegation run still creates its own `FileLockRegistry` for file identity
# (unchanged — see the module docstring); code that wants leases visible here
# passes `get_registry()` in alongside it, or mirrors acquire/release calls
# into it. Lazy so importing this module never allocates one that nothing
# uses (e.g. a unit test that only exercises `ResourceOwnershipRegistry`
# directly, as tests/test_resource_ownership.py does with its own instances).
_registry: Optional["ResourceOwnershipRegistry"] = None


def get_registry() -> "ResourceOwnershipRegistry":
    """The process-wide `ResourceOwnershipRegistry` — created on first use."""
    global _registry
    if _registry is None:
        _registry = ResourceOwnershipRegistry()
    return _registry


def reset_registry() -> None:
    """Replace the process-wide registry with a fresh, empty one. For tests
    only — production code never needs to reset shared state mid-run."""
    global _registry
    _registry = ResourceOwnershipRegistry()
