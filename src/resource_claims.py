"""Resource claims for parallel tool calls (H18).

Before tool calls run side by side, each one says what it touches: a file, a
directory tree, a process, an external resource, or (when it cannot say) the
whole workspace. Two claims are compatible unless they overlap and at least one
of them writes. The claim checks live in one place so the loop that groups
calls, the broker that holds claims across runs, and the preview/apply guard
all agree on what "conflict" means.

Compatibility matrix (``R`` read, ``W`` write, both on overlapping resources):

    +---+---+---+
    |   | R | W |
    +---+---+---+
    | R | ok| x |
    | W | x | x |
    +---+---+---+

Overlap: two paths overlap when they are equal; a tree overlaps every path and
tree inside it; the workspace claim overlaps every path and tree under the
workspace root (and other workspace claims on the same root); process and
external claims overlap only claims of the same kind and key. A ``wildcard``
claim overlaps everything.

What is deliberately not here: claims are process-local, not an OS lock.
Another process, a shell command that writes files, or a hard link to the same
file are outside what they can see; the preview/apply fingerprint below is the
only guard against those.
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SCOPES = ("off", "files", "files_and_external")
DEFAULT_SCOPE = "files"
DEFAULT_WAIT_SECONDS = 120.0


class Mode(str, enum.Enum):
    READ = "read"
    WRITE = "write"


KIND_PATH = "path"
KIND_TREE = "tree"
KIND_WORKSPACE = "workspace"
KIND_PROCESS = "process"
KIND_EXTERNAL = "external"
KIND_WILDCARD = "wildcard"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _inside(child: str, parent: str) -> bool:
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class Resource:
    kind: str
    key: str

    def label(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass(frozen=True)
class Claim:
    resource: Resource
    mode: Mode
    reason: str = ""

    def label(self) -> str:
        return f"{self.mode.value} {self.resource.label()}"


def path_claim(path: str, mode: Mode, reason: str = "") -> Claim:
    return Claim(Resource(KIND_PATH, _norm(path)), mode, reason)


def tree_claim(path: str, mode: Mode, reason: str = "") -> Claim:
    return Claim(Resource(KIND_TREE, _norm(path)), mode, reason)


def workspace_claim(root: str, mode: Mode, reason: str = "") -> Claim:
    return Claim(Resource(KIND_WORKSPACE, _norm(root or os.getcwd())), mode, reason)


def process_claim(process_id: str, mode: Mode = Mode.WRITE, reason: str = "") -> Claim:
    return Claim(Resource(KIND_PROCESS, str(process_id)), mode, reason)


def external_claim(name: str, mode: Mode, reason: str = "") -> Claim:
    return Claim(Resource(KIND_EXTERNAL, str(name)), mode, reason)


def wildcard_claim(mode: Mode = Mode.WRITE, reason: str = "") -> Claim:
    return Claim(Resource(KIND_WILDCARD, "*"), mode, reason)


_FILE_KINDS = (KIND_PATH, KIND_TREE, KIND_WORKSPACE)


def overlaps(a: Resource, b: Resource) -> bool:
    if KIND_WILDCARD in (a.kind, b.kind):
        return True
    if a.kind in _FILE_KINDS and b.kind in _FILE_KINDS:
        if a.kind == KIND_PATH and b.kind == KIND_PATH:
            return a.key == b.key
        return _inside(a.key, b.key) or _inside(b.key, a.key)
    if a.kind == b.kind:
        return a.key == b.key
    return False


def conflicts(a: Claim, b: Claim) -> bool:
    """True when the two claims cannot be held at the same time."""
    if a.mode is Mode.READ and b.mode is Mode.READ:
        return False
    return overlaps(a.resource, b.resource)


def first_conflict(claims: Sequence[Claim], others: Iterable[Claim]) -> Optional[Tuple[Claim, Claim]]:
    others = list(others)
    for mine in claims:
        for theirs in others:
            if conflicts(mine, theirs):
                return mine, theirs
    return None


class ClaimSet:
    """Claims of a group of calls that are about to start together."""

    def __init__(self) -> None:
        self._claims: List[Claim] = []

    def conflict_for(self, claims: Sequence[Claim]) -> Optional[Tuple[Claim, Claim]]:
        return first_conflict(claims, self._claims)

    def try_add(self, claims: Sequence[Claim]) -> bool:
        """Add ``claims`` when none conflicts with what the set holds."""
        if self.conflict_for(claims) is not None:
            return False
        self._claims.extend(claims)
        return True

    def __len__(self) -> int:
        return len(self._claims)

    def labels(self) -> List[str]:
        return [c.label() for c in self._claims]


# ── deriving claims from a tool call ─────────────────────────────────────────

#: Tools whose ``path`` argument names a directory to read.
_TREE_READ_TOOLS = frozenset({"ls", "glob", "grep", "list_dir", "search_files"})
_PROCESS_KEYS = ("process_id", "pid", "task_id", "job_id")


def _json_args(content: str) -> Dict[str, Any]:
    try:
        data = json.loads(content or "")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(path: str, workspace: Optional[str]) -> str:
    if os.path.isabs(path) or not workspace:
        return path
    return os.path.join(workspace, path)


def claims_for_call(tool_type: str, content: str, workspace: Optional[str] = None,
                    scope: str = DEFAULT_SCOPE) -> List[Claim]:
    """What a tool call touches, as claims. Empty means "claims say nothing".

    * a read of named paths -> read claims (a tree when the path is a directory
      or the tool lists/searches one);
    * a read that names nothing inside the workspace -> a workspace read;
    * a file write of named paths -> write claims;
    * a file write whose paths cannot be determined (the preview has no
      recognisable target) -> a workspace write, the conservative claim;
    * with ``scope="files_and_external"``: a call to an MCP server claims that
      server, and a call naming a process id claims the process.

    Calls whose effects are unknown, or that run commands, claim nothing: the
    claims would not be able to say what a shell command touches.
    """
    if scope == "off":
        return []
    try:
        from src.tool_capabilities import ToolEffect, capabilities_for_action
        caps = capabilities_for_action(tool_type, content)
    except Exception:  # noqa: BLE001 - unknown stays unclaimed, as before
        return []
    known = bool(getattr(caps, "known", False)) and bool(caps.effects)
    reads = {ToolEffect.READ_PUBLIC, ToolEffect.READ_WORKSPACE, ToolEffect.READ_PRIVATE}
    effects = set(caps.effects or ())
    is_read = known and effects <= reads
    is_file_write = known and effects == {ToolEffect.WRITE_WORKSPACE}

    out: List[Claim] = []
    if is_read or is_file_write:
        try:
            from src import agent_harness as _ah
            raw_paths = _ah._paths_from_args(tool_type, content or "")
        except Exception:  # noqa: BLE001
            raw_paths = []
        mode = Mode.READ if is_read else Mode.WRITE
        if raw_paths:
            for raw in raw_paths:
                full = _resolve(raw, workspace)
                tree = mode is Mode.READ and (tool_type in _TREE_READ_TOOLS or os.path.isdir(full))
                out.append((tree_claim if tree else path_claim)(full, mode, f"{tool_type} {raw}"))
        elif mode is Mode.WRITE:
            # Unknown preview: nothing tells which files the apply will touch.
            out.append(workspace_claim(workspace or os.getcwd(), Mode.WRITE,
                                       f"{tool_type}: targets not determinable"))
        elif ToolEffect.READ_WORKSPACE in effects:
            out.append(workspace_claim(workspace or os.getcwd(), Mode.READ, f"{tool_type}: whole workspace"))

    if scope == "files_and_external":
        if tool_type.startswith("mcp__"):
            # A tool the capability table cannot classify is treated as a write
            # to its server: the conservative reading.
            parts = tool_type.split("__")
            server = parts[1] if len(parts) > 2 else tool_type
            out.append(external_claim(f"mcp:{server}", Mode.READ if is_read else Mode.WRITE, tool_type))
        args = _json_args(content)
        for key in _PROCESS_KEYS:
            if args.get(key):
                out.append(process_claim(str(args[key]), Mode.WRITE, f"{tool_type} {key}"))
                break
    return out


def scope_setting() -> str:
    try:
        from src.settings import get_setting
        raw = str(get_setting("agent_resource_claims", DEFAULT_SCOPE) or DEFAULT_SCOPE).strip().lower()
    except Exception:  # noqa: BLE001
        return DEFAULT_SCOPE
    return raw if raw in SCOPES else DEFAULT_SCOPE


def wait_seconds_setting() -> float:
    try:
        from src.settings import get_setting
        value = float(get_setting("agent_resource_claim_wait_s", DEFAULT_WAIT_SECONDS))
    except Exception:  # noqa: BLE001
        return DEFAULT_WAIT_SECONDS
    return value if value > 0 else DEFAULT_WAIT_SECONDS


# ── the broker ───────────────────────────────────────────────────────────────

class ClaimTimeout(Exception):
    """A claim set could not be granted before the wait limit."""

    def __init__(self, message: str, blockers: List[str]):
        super().__init__(message)
        self.blockers = blockers


@dataclass
class _Holder:
    lease_id: int
    owner: str
    label: str
    claims: Tuple[Claim, ...]
    granted_at: float


@dataclass
class _Waiter:
    lease_id: int
    owner: str
    label: str
    claims: Tuple[Claim, ...]
    loop: asyncio.AbstractEventLoop
    future: "asyncio.Future[None]"
    enqueued_at: float


@dataclass
class Lease:
    lease_id: int
    claims: Tuple[Claim, ...]
    waited_s: float = 0.0


class ClaimBroker:
    """Grants claim sets atomically and in arrival order.

    A set is granted only when none of its claims conflicts with a held claim
    or with an earlier waiter's claims, so a stream of readers cannot starve a
    writer and a set never holds part of what it asked for (no hold-and-wait,
    hence no deadlock between runs). Thread-safe: waiters may live in different
    event loops. Never hold a lease across a call that can itself claim.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Dict[int, _Holder] = {}
        self._waiting: Deque[_Waiter] = deque()
        self._next = 1
        self.granted = 0
        self.waited = 0
        self.total_wait_s = 0.0
        self.max_concurrent = 0
        self.timeouts = 0

    # -- internals (caller holds the lock) ---------------------------------

    def _blocked_by(self, claims: Sequence[Claim], before: Optional[_Waiter]) -> List[str]:
        blockers: List[str] = []
        for holder in self._held.values():
            hit = first_conflict(claims, holder.claims)
            if hit:
                blockers.append(f"{holder.label or holder.owner} holds {hit[1].label()}")
        for waiter in self._waiting:
            if waiter is before:
                break
            hit = first_conflict(claims, waiter.claims)
            if hit:
                blockers.append(f"{waiter.label or waiter.owner} is waiting for {hit[1].label()}")
        return blockers

    def _grant_ready(self) -> List[_Waiter]:
        ready: List[_Waiter] = []
        for waiter in list(self._waiting):
            if self._blocked_by(waiter.claims, waiter):
                continue
            self._waiting.remove(waiter)
            self._held[waiter.lease_id] = _Holder(
                waiter.lease_id, waiter.owner, waiter.label, waiter.claims, time.monotonic())
            self.max_concurrent = max(self.max_concurrent, len(self._held))
            ready.append(waiter)
        return ready

    @staticmethod
    def _wake(waiters: List[_Waiter]) -> None:
        for waiter in waiters:
            def _set(w=waiter):
                if not w.future.done():
                    w.future.set_result(None)
            try:
                waiter.loop.call_soon_threadsafe(_set)
            except RuntimeError:  # loop closed: the waiter is gone
                pass

    # -- API ---------------------------------------------------------------

    async def acquire(self, claims: Sequence[Claim], *, owner: str = "", label: str = "",
                      timeout: Optional[float] = None) -> Lease:
        claims_t = tuple(claims)
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        with self._lock:
            lease_id = self._next
            self._next += 1
            if not claims_t:
                self.granted += 1
                return Lease(lease_id, claims_t, 0.0)
            if not self._blocked_by(claims_t, None):
                self._held[lease_id] = _Holder(lease_id, owner, label, claims_t, now)
                self.granted += 1
                self.max_concurrent = max(self.max_concurrent, len(self._held))
                return Lease(lease_id, claims_t, 0.0)
            waiter = _Waiter(lease_id, owner, label, claims_t, loop, loop.create_future(), now)
            self._waiting.append(waiter)
            blockers = self._blocked_by(claims_t, waiter)
        logger.debug("[claims] %s waits: %s", label or owner, blockers)
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except BaseException as exc:
            with self._lock:
                granted_meanwhile = lease_id in self._held
                if waiter in self._waiting:
                    self._waiting.remove(waiter)
                if granted_meanwhile:
                    self._held.pop(lease_id, None)
                blockers = self._blocked_by(claims_t, None)
                ready = self._grant_ready()
            self._wake(ready)
            if isinstance(exc, asyncio.TimeoutError):
                self.timeouts += 1
                raise ClaimTimeout(
                    f"resource claims not granted within {timeout:g}s", blockers) from None
            raise
        waited = time.monotonic() - now
        with self._lock:
            self.granted += 1
            self.waited += 1
            self.total_wait_s += waited
        return Lease(lease_id, claims_t, waited)

    def release(self, lease: Lease) -> None:
        with self._lock:
            self._held.pop(lease.lease_id, None)
            ready = self._grant_ready()
        self._wake(ready)

    @asynccontextmanager
    async def hold(self, claims: Sequence[Claim], *, owner: str = "", label: str = "",
                   timeout: Optional[float] = None):
        lease = await self.acquire(claims, owner=owner, label=label, timeout=timeout)
        try:
            yield lease
        finally:
            self.release(lease)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "held": [{"owner": h.owner, "label": h.label,
                          "claims": [c.label() for c in h.claims],
                          "held_s": round(now - h.granted_at, 3)} for h in self._held.values()],
                "waiting": [{"owner": w.owner, "label": w.label,
                             "claims": [c.label() for c in w.claims],
                             "waiting_s": round(now - w.enqueued_at, 3),
                             "blocked_by": self._blocked_by(w.claims, w)} for w in self._waiting],
                "stats": {"granted": self.granted, "waited": self.waited,
                          "total_wait_s": round(self.total_wait_s, 4),
                          "max_concurrent": self.max_concurrent, "timeouts": self.timeouts},
            }


_BROKER = ClaimBroker()


def broker() -> ClaimBroker:
    return _BROKER


def reset_broker_for_tests() -> ClaimBroker:
    global _BROKER
    _BROKER = ClaimBroker()
    return _BROKER


def blocked_by_claims_result(tool_type: str, exc: ClaimTimeout) -> Tuple[str, Dict[str, Any]]:
    """The tool result a call gets when its claims could not be granted."""
    return (
        f"{tool_type}: BLOCKED",
        {
            "error": f"{tool_type}: another call holds what this one needs ({'; '.join(exc.blockers) or 'claims'}); "
                     "it was not started. Retry when the other work has finished.",
            "exit_code": 1, "status": "denied", "error_code": "RESOURCE_CLAIM_TIMEOUT",
            "source": "resource_claims", "blockers": list(exc.blockers), "blocked": True,
        },
    )


async def run_with_claims(block: Any, workspace: Optional[str], factory: Callable[[], Awaitable[Any]],
                          *, owner: str = "") -> Any:
    """Await ``factory()`` while holding the claims of ``block``.

    With the feature off, or when the call names nothing, it is just
    ``await factory()``. A call that cannot get its claims in time is not
    started and returns a denied result instead.
    """
    scope = scope_setting()
    tool_type = str(getattr(block, "tool_type", "") or "")
    claims = claims_for_call(tool_type, getattr(block, "content", "") or "", workspace, scope) if scope != "off" else []
    if not claims:
        return await factory()
    try:
        async with _BROKER.hold(claims, owner=owner, label=tool_type, timeout=wait_seconds_setting()):
            return await factory()
    except ClaimTimeout as exc:
        return blocked_by_claims_result(tool_type, exc)


# ── preview / apply: external writer detection ───────────────────────────────

@dataclass(frozen=True)
class FileFingerprint:
    """Identity of a file at one moment: existence, size, mtime and content hash.

    ``known`` is False when the file could not be examined for a reason other
    than not existing; such a fingerprint proves nothing and never matches.
    """

    known: bool
    exists: bool = False
    size: Optional[int] = None
    mtime_ns: Optional[int] = None
    sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"known": self.known, "exists": self.exists, "size": self.size,
                "mtime_ns": self.mtime_ns, "sha256": self.sha256}


def fingerprint(path: str, *, with_hash: bool = True, max_hash_bytes: int = 64 * 1024 * 1024) -> FileFingerprint:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return FileFingerprint(known=True, exists=False)
    except OSError:
        return FileFingerprint(known=False)
    digest = None
    if with_hash and st.st_size <= max_hash_bytes:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        except FileNotFoundError:
            return FileFingerprint(known=True, exists=False)
        except OSError:
            return FileFingerprint(known=False)
    return FileFingerprint(known=True, exists=True, size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=digest)


def stat_identity(path: str) -> FileFingerprint:
    """Cheap fingerprint (no content hash) for callers that already hold a content revision."""
    return fingerprint(path, with_hash=False)


def drift(expected: FileFingerprint, current: FileFingerprint) -> List[str]:
    """Names of the fields that differ; empty means nothing changed.

    An expected fingerprint that is not known never matches: the guard cannot
    claim "unchanged" about something it could not see. Fields missing on
    either side (for example no hash) are not compared.
    """
    if not expected.known or not current.known:
        return ["unknown"]
    if expected.exists != current.exists:
        return ["exists"]
    if not expected.exists:
        return []
    changed = []
    if expected.size != current.size:
        changed.append("size")
    if expected.mtime_ns != current.mtime_ns:
        changed.append("mtime_ns")
    if expected.sha256 is not None and current.sha256 is not None and expected.sha256 != current.sha256:
        changed.append("sha256")
    return changed


def external_writer_conflict(tool: str, path: str, expected: FileFingerprint,
                             current: FileFingerprint, changed: Sequence[str]) -> Dict[str, Any]:
    """The refusal a guarded apply returns when the file changed after its preview."""
    return {
        "error": f"{tool}: {path} was changed by another writer after the preview was read "
                 f"({', '.join(changed)} differ); nothing was written",
        "exit_code": 1, "status": "conflict", "error_code": "EXTERNAL_WRITER_DETECTED",
        "source": "external_writer", "changed": list(changed),
        "preview_identity": expected.to_dict(), "current_identity": current.to_dict(),
        "next_action": "read_current_and_reconcile",
    }


class PreviewBinding:
    """Fingerprints of the files a preview was made from, checked again at apply.

    ``capture`` runs at preview time. ``verify`` runs at apply time, inside
    whatever exclusion the caller holds, and returns the first file that
    changed. A path whose preview could not be fingerprinted is reported as
    ``unknown``: the caller decides whether that refuses the apply; the claims
    derived for such a call are the conservative workspace write.
    """

    def __init__(self) -> None:
        self._expected: Dict[str, FileFingerprint] = {}

    def capture(self, paths: Iterable[str], *, with_hash: bool = True) -> "PreviewBinding":
        for path in paths:
            self._expected[path] = fingerprint(path, with_hash=with_hash)
        return self

    def unknown_paths(self) -> List[str]:
        return [p for p, fp in self._expected.items() if not fp.known]

    def verify(self, *, with_hash: bool = True) -> Optional[Tuple[str, FileFingerprint, FileFingerprint, List[str]]]:
        for path, expected in self._expected.items():
            if not expected.known:
                continue
            current = fingerprint(path, with_hash=with_hash)
            changed = drift(expected, current)
            if changed:
                return path, expected, current, changed
        return None
