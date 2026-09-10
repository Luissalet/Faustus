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
            "acquired_at": self.acquired_at, "ttl_seconds": self.ttl_seconds,
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
                ttl_seconds: Optional[float] = None, now: Optional[float] = None) -> bool:
        """Claim `resource` for `owner`.

        Returns `False` when a DIFFERENT owner already holds an unexpired
        lease on it. An expired lease is reaped (and reconciled) first, so a
        holder that died without releasing never blocks a new owner forever.
        The same owner re-acquiring what it already holds renews it instead
        of failing — a subagent polling its own lease should not have to
        special-case "I already have this".
        """
        key = self._key(kind, resource)
        self._reap_if_expired(key, now=now)
        moment = now if now is not None else time.time()
        current = self._leases.get(key)
        if current is not None:
            if current.owner == owner:
                current.renewed_at = moment
                return True
            return False
        if kind == "file":
            taken = self.files.claim(owner, [resource])
            if taken:
                return False
        self._leases[key] = Lease(resource=resource, kind=kind, owner=owner,
                                  acquired_at=moment,
                                  ttl_seconds=ttl_seconds if ttl_seconds is not None
                                  else self.default_ttl_seconds)
        return True

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
