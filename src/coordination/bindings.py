"""Immutable, revocable coordination scope established by the product owner."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass


class CoordinationError(ValueError):
    pass


class StaleBinding(CoordinationError):
    pass


class UnknownOutcome(CoordinationError):
    """A dispatched write cannot be automatically retried after losing its result."""
    pass


READ_OPERATIONS = frozenset({"poll", "detail", "task", "board"})
WRITE_OPERATIONS = frozenset({"heartbeat", "add", "claim", "checkpoint", "post", "submit", "review", "done", "lock", "unlock"})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS


def resource(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoordinationError("resource must be a non-empty string")
    value = value.strip().replace("\\", "/").casefold().rstrip("/")
    scheme, _, target = value.partition(":")
    if (scheme not in {"path", "repo", "model", "gpu", "port", "app", "merge"}
            or not target or ":" in target or "//" in target
            or any(p in {".", ".."} for p in target.split("/"))):
        raise CoordinationError("invalid coordination resource")
    if len(value) > 512 or any(ord(c) < 32 for c in value) or any(c in value for c in "*?%"):
        raise CoordinationError("invalid coordination resource")
    return value


@dataclass(frozen=True)
class Binding:
    id: str
    owner: str
    project_id: str
    session_id: str
    connector_id: str
    actor: str
    operations: tuple[str, ...]
    resources: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    thread_ids: tuple[int, ...] = ()
    connector_fingerprint: str = ""
    revision: int = 1
    active: bool = True
    template_id: str | None = None
    template_revision: int | None = None
    space_id: str = 'ecosystem'

    def __post_init__(self):
        from src.hoard_link.coordination.spaces import space_id
        try:
            object.__setattr__(self, 'space_id', space_id(self.space_id))
        except ValueError as error:
            raise CoordinationError(str(error)) from error
        for field in ("id", "project_id", "session_id", "connector_id"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise CoordinationError(f"{field} is required")
        if not isinstance(self.owner, str):
            raise CoordinationError("owner must come from the runtime")
        if not isinstance(self.actor, str) or not re.fullmatch(r"faustus(?:[-_][a-z0-9][a-z0-9_-]{0,38})?", self.actor):
            raise CoordinationError("coordination actor must identify Faustus")
        if type(self.revision) is not int or self.revision < 1 or type(self.active) is not bool:
            raise CoordinationError("invalid binding revision or state")
        if (self.template_id is None) != (self.template_revision is None) or (self.template_id is not None and
                (not isinstance(self.template_id, str) or not self.template_id or type(self.template_revision) is not int or self.template_revision < 1)):
            raise CoordinationError("invalid project coordination revision")
        for key in ("operations", "resources", "repos", "thread_ids"):
            values = getattr(self, key)
            if not isinstance(values, (tuple, list)) or len(values) > 100:
                raise CoordinationError(f"invalid {key} scope")
        if not all(isinstance(op, str) and op in OPERATIONS for op in self.operations):
            raise CoordinationError("invalid operation scope")
        object.__setattr__(self, "operations", tuple(sorted(set(self.operations))))
        object.__setattr__(self, "resources", tuple(sorted({resource(r) for r in self.resources})))
        if not all(isinstance(r, str) and r.strip() and len(r) <= 200
                   and not any(c in r for c in ":/\\\r\n\0") for r in self.repos):
            raise CoordinationError("invalid repository scope")
        object.__setattr__(self, "repos", tuple(sorted({r.strip().casefold() for r in self.repos})))
        if not all(type(t) is int and t > 0 for t in self.thread_ids):
            raise CoordinationError("invalid thread scope")
        object.__setattr__(self, "thread_ids", tuple(sorted(set(self.thread_ids))))

    def permits_resource(self, value: str) -> bool:
        try:
            wanted = resource(value)
        except CoordinationError:
            return False
        return any(wanted == root or (root.startswith("path:") and wanted.startswith(root + "/"))
                   for root in self.resources)

    def serialise(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectTemplate:
    id: str
    owner: str
    project_id: str
    connector_id: str
    actor: str
    operations: tuple[str, ...]
    resources: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    thread_ids: tuple[int, ...] = ()
    connector_fingerprint: str = ""
    revision: int = 1
    active: bool = True
    space_id: str = 'ecosystem'

    def __post_init__(self):
        probe = Binding(session_id="project:" + self.project_id, **self.serialise())
        for key in ("operations", "resources", "repos", "thread_ids", "space_id"):
            object.__setattr__(self, key, getattr(probe, key))

    def serialise(self):
        return asdict(self)

    def for_session(self, session_id, *, identifier=None):
        import hashlib
        import uuid
        # One stable identity per chat, without asking the user for an id.
        # Separate chats must not renew or claim each other's actor locks.
        actor = "faustus-" + hashlib.sha256((self.owner + "\0" + self.id + "\0" + session_id).encode()).hexdigest()[:20]
        return Binding(**{**self.serialise(), "id": identifier or uuid.uuid4().hex,
                          "session_id": session_id, "actor": actor, "revision": 1,
                          "template_id": self.id, "template_revision": self.revision})
