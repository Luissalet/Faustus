"""
project_context/models.py — the typed vocabulary of a project's knowledge links.

Why these are contracts and not dicts
-------------------------------------
The failure this module exists to prevent is not a typo. A resolver asked about
a document belonging to somebody else answered::

    {"state": "forbidden", "label": "Q3 layoff plan", "byte_size": 41233}

The ownership check worked. The refusal leaked the exact thing the check was
protecting. A ``forbidden`` that names the document has already given away the
document.

So ``SourceMetadata`` does not trust the resolver that builds it. A state that
is not ``ok`` is scrubbed on the way in — label, canonical ref, media type,
size, owner, revision and version are blanked — and the scrub says in the log
which fields it had to drop. A resolver that forgets the rule cannot leak
through this type, and the resolver that forgets shows up in a log line instead
of in somebody else's chat.

Everything else follows ``src/contracts/base.py``: a rejection names the field
and the value it saw, an unknown key is an error rather than a silent default,
and nothing is coerced across a type boundary. Those helpers are imported from
there, never restated here.

``ProjectContextError`` extends ``ContractError`` (itself a ``ValueError``), so
one ``except`` clause covers both this module's rejections and the ones raised
by the shared readers it delegates to.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from src.contracts.base import (
    ContractError, as_mapping, flag, one_of, reject_unknown, text, text_list, whole,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACCESS_MODES", "ACTOR_KINDS", "INDEX_STATUSES", "LINK_KINDS", "PATCHABLE_FIELDS",
    "PATH_KINDS", "REF_KINDS", "RELATIONS", "RETRIEVAL_POLICIES", "ROLES",
    "SOURCE_STATES", "VERSION_POLICIES",
    "ActorRef", "AttachResult", "ContextLinkStatus", "DetachResult",
    "ExtractedChunk", "ExtractedCorpus", "ProjectContextError", "ProjectContextLink",
    "RefreshResult", "SourceContent", "SourceMatch", "SourceMetadata", "SourceRef",
]


#: The source types a link can point at. ``chat`` is deliberately absent: the
#: plan defers it, and a kind with no resolver is a link nothing can read.
LINK_KINDS: Tuple[str, ...] = ("file", "folder", "document", "artifact", "gallery_image")

#: Which revision of the source the link stands for. ``snapshot`` is accepted
#: by the contract and refused by the resolvers until an Artifact can be
#: materialised for it — see ``resolvers/document.py``.
VERSION_POLICIES: Tuple[str, ...] = ("latest", "pinned", "snapshot")

#: When a link may enter a prompt. There is no ``always_full`` on purpose: a
#: long document injected every turn spends the window and degrades the model.
RETRIEVAL_POLICIES: Tuple[str, ...] = ("auto", "pinned_summary", "on_demand", "disabled")

#: A retrieval signal, never a permission. A ``requirements`` link is ranked
#: differently from an ``archive`` one; neither grants access to anything.
ROLES: Tuple[str, ...] = (
    "requirements", "reference", "decision", "style_reference", "example",
    "dataset", "specification", "output", "archive",
)

INDEX_STATUSES: Tuple[str, ...] = ("none", "queued", "indexing", "ready", "stale", "failed")

#: Membership in a project's context is not permission to edit the source.
#: ``read_only`` is the default and does not widen the run's work roots.
ACCESS_MODES: Tuple[str, ...] = ("read_only", "work_root")

#: What a resolver may say about a source. Only ``ok`` carries information.
SOURCE_STATES: Tuple[str, ...] = ("ok", "missing", "forbidden", "unsupported")

ACTOR_KINDS: Tuple[str, ...] = ("user", "agent", "workflow", "system")

#: How a turn reference came to exist (``references.py``).
RELATIONS: Tuple[str, ...] = ("created", "opened", "uploaded", "mentioned", "generated")

#: Kinds located by a filesystem path rather than a row id, and vice versa.
PATH_KINDS: Tuple[str, ...] = ("file", "folder")
REF_KINDS: Tuple[str, ...] = ("document", "artifact", "gallery_image")

#: The only link fields ``ProjectContextService.update`` will change. Identity,
#: provenance and revision are not in it: a patch that could rewrite ``ref_id``
#: would let an update point an approved link at a different source.
PATCHABLE_FIELDS: Tuple[str, ...] = (
    "label", "role", "tags", "summary", "retrieval_policy", "version_policy",
    "pinned_version", "access_mode", "enabled",
)


class ProjectContextError(ContractError):
    """A rejection that names the field and the value that caused it.

    Subclasses ``ContractError`` (a ``ValueError``) so that callers catching
    either type catch both, and so the message format is the one the rest of
    Faustus already prints.
    """


@contextlib.contextmanager
def _own_errors():
    """Re-raise the shared readers' ``ContractError`` as this module's type, so
    one ``except ProjectContextError`` covers a whole ``parse()``."""
    try:
        yield
    except ProjectContextError:
        raise
    except ContractError as exc:
        extra = {"got": exc.got} if exc.has_got else {}
        raise ProjectContextError(exc.path, exc.message, **extra) from None


# ── what the caller points at, and who is pointing ─────────────────────────

@dataclass(frozen=True)
class SourceRef:
    """A locator the resolvers understand: a row id, or a filesystem path.

    The tool layer resolves deictic shortcuts (``active_document`` and friends)
    through ``references.TurnReferenceRegistry`` *before* building one of these,
    so a ``SourceRef`` always names a concrete entity. ``kind`` is closed to
    ``LINK_KINDS`` for the same reason: a kind nothing resolves is a link that
    can never be read back.
    """

    kind: str
    id: str = ""
    path: str = ""

    _KEYS = ("kind", "id", "path")

    @classmethod
    def parse(cls, raw: Any, path: str = "source") -> "SourceRef":
        with _own_errors():
            data = as_mapping(raw, path)
            reject_unknown(data, cls._KEYS, path)
            kind = one_of(data, "kind", path, choices=LINK_KINDS)
            ref_id = text(data, "id", path, required=False, max_len=256)
            ref_path = text(data, "path", path, required=False, max_len=4096)
            if kind in PATH_KINDS and not ref_path:
                raise ProjectContextError(f"{path}.path", f"is required for kind {kind!r}")
            if kind in REF_KINDS and not ref_id:
                raise ProjectContextError(f"{path}.id", f"is required for kind {kind!r}")
            return cls(kind=kind, id=ref_id, path=ref_path)

    @property
    def locator(self) -> str:
        """The half of the ref this kind actually uses, for messages and keys."""
        return self.path if self.kind in PATH_KINDS else self.id

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "path": self.path}


@dataclass(frozen=True)
class ActorRef:
    """Who asked. Recorded for provenance; it never decides an outcome.

    Ownership is compared against the session, the project and the source, not
    against this — an actor that could name its own owner would be a way to
    ask for somebody else's document politely.
    """

    kind: str
    name: str = ""
    session_id: str = ""
    run_id: str = ""

    _KEYS = ("kind", "name", "session_id", "run_id")

    @classmethod
    def parse(cls, raw: Any, path: str = "actor") -> "ActorRef":
        with _own_errors():
            data = as_mapping(raw, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(
                kind=one_of(data, "kind", path, choices=ACTOR_KINDS),
                name=text(data, "name", path, required=False, max_len=128),
                session_id=text(data, "session_id", path, required=False, max_len=128),
                run_id=text(data, "run_id", path, required=False, max_len=128),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name,
                "session_id": self.session_id, "run_id": self.run_id}


# ── what a resolver is allowed to say back ─────────────────────────────────

#: Fields that carry information about the source itself. Every one of them is
#: blanked when ``state`` is not ``ok``; see the class docstring.
_REVEALING_FIELDS = ("canonical_ref", "label", "media_type", "byte_size", "owner",
                     "revision", "version")


@dataclass(frozen=True)
class SourceMetadata:
    """What a resolver knows about a source — and nothing at all when it must
    refuse.

    ``state`` is the whole contract. ``ok`` means the caller owns the source and
    it exists, and the descriptive fields are populated. ``missing``,
    ``forbidden`` and ``unsupported`` mean the caller learns the state and not
    one byte more: the constructor blanks label, canonical ref, media type,
    size, owner, revision and version, and logs which fields it had to drop.

    That scrub is not defence in depth for its own sake. A resolver is the one
    place in this subsystem that holds another user's row in a local variable,
    and "return early with a helpful message" is the natural thing to write.
    Making the type refuse to carry the message is cheaper than reviewing every
    resolver forever.

    ``extra`` survives a refusal, but only as a single ``note`` key holding a
    short, source-independent explanation ("not found", "not owned by the
    caller"). Anything else in it is dropped with the rest.
    """

    state: str = "ok"
    kind: str = ""
    canonical_ref: str = ""
    label: str = ""
    media_type: str = ""
    byte_size: int = 0
    owner: str = ""
    revision: str = ""
    version: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    _KEYS = ("state", "kind", "canonical_ref", "label", "media_type", "byte_size",
             "owner", "revision", "version", "extra")

    def __post_init__(self) -> None:
        if self.state not in SOURCE_STATES:
            # Not a raise: this runs on the read path of every retrieval, and an
            # unreadable state must degrade to the safest one, not kill a turn.
            logger.warning("project_context: unknown source state %r, treating as forbidden",
                           self.state)
            object.__setattr__(self, "state", "forbidden")
        if self.state == "ok":
            object.__setattr__(self, "extra", dict(self.extra or {}))
            return
        dropped = [name for name in _REVEALING_FIELDS if getattr(self, name)]
        for name in _REVEALING_FIELDS:
            object.__setattr__(self, name, 0 if name in ("byte_size", "version") else "")
        note = str((self.extra or {}).get("note") or "").strip()[:200]
        if self.extra and set(self.extra) - {"note"}:
            dropped.extend(sorted(set(self.extra) - {"note"}))
        object.__setattr__(self, "extra", {"note": note} if note else {})
        if dropped:
            logger.warning(
                "project_context: scrubbed %s from a %r %s result; a refusal that names "
                "the source has already leaked it", sorted(set(dropped)), self.state,
                self.kind or "source",
            )

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def note(self) -> str:
        return str((self.extra or {}).get("note") or "")

    @classmethod
    def denied(cls, kind: str, state: str, note: str = "") -> "SourceMetadata":
        """The only constructor a resolver should use to refuse. Nothing about
        the source can reach it, because nothing about the source is passed."""
        return cls(state=state, kind=kind, extra={"note": note} if note else {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state, "kind": self.kind, "canonical_ref": self.canonical_ref,
            "label": self.label, "media_type": self.media_type,
            "byte_size": self.byte_size, "owner": self.owner, "revision": self.revision,
            "version": self.version, "extra": dict(self.extra or {}),
        }


@dataclass(frozen=True)
class SourceContent:
    """A bounded window of a source, stamped with the revision it came from.

    ``truncated`` is not cosmetic. A model handed the first 200 KB of a 4 MB
    file and told nothing will answer as if it read the file, and the answer is
    unfalsifiable. ``start``/``end``/``total`` let the caller say "characters
    120-4200 of 91300" and let the next call continue.
    """

    text: str = ""
    start: int = 0
    end: int = 0
    total: int = 0
    truncated: bool = False
    revision: str = ""
    media_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "start": self.start, "end": self.end,
            "total": self.total, "truncated": self.truncated,
            "revision": self.revision, "media_type": self.media_type,
        }


@dataclass(frozen=True)
class SourceMatch:
    """One hit inside a source. ``location`` is whatever locates it in that
    source's own coordinates — a version and line for a document, a path and
    line for a file, a page for a PDF — so provenance can quote it back."""

    location: Mapping[str, Any] = field(default_factory=dict)
    snippet: str = ""
    score: float = 0.0
    revision: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"location": dict(self.location or {}), "snippet": self.snippet,
                "score": self.score, "revision": self.revision}


@dataclass(frozen=True)
class ExtractedChunk:
    """A unit the indexer can embed, carrying enough location to cite it."""

    index: int = 0
    text: str = ""
    title: str = ""
    location: Mapping[str, Any] = field(default_factory=dict)
    media_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "text": self.text, "title": self.title,
                "location": dict(self.location or {}), "media_type": self.media_type}


@dataclass(frozen=True)
class ExtractedCorpus:
    """Everything extractable from one source at one revision.

    ``degraded`` is the honest half. Office extraction goes through the
    *optional* markitdown dependency; when it is absent the corpus is what the
    built-in path could read, and saying so beats indexing a partial document
    as if it were the whole one.
    """

    revision: str = ""
    chunks: Tuple[ExtractedChunk, ...] = ()
    degraded: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"revision": self.revision, "degraded": self.degraded, "note": self.note,
                "chunks": [c.to_dict() for c in self.chunks]}


# ── the link itself ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProjectContextLink:
    """One durable, typed reference from a project to a source it knows.

    A link is membership and policy, never content and never permission. It
    says *this source belongs to this project, at this revision, under this
    retrieval policy*. It does not grant access — every read re-checks
    ownership against the source itself, because a label stored in
    ``projects.json`` is a value an earlier bug could have written.
    """

    id: str
    kind: str
    ref_id: str = ""
    path: str = ""
    label: str = ""
    media_type: str = ""
    version_policy: str = "latest"
    pinned_version: Optional[int] = None
    retrieval_policy: str = "auto"
    role: str = "reference"
    tags: Tuple[str, ...] = ()
    summary: str = ""
    summary_revision: str = ""
    created_by: str = ""
    created_from_session_id: str = ""
    created_from_run_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    content_revision: str = ""
    index_status: str = "none"
    index_revision: str = ""
    access_mode: str = "read_only"
    enabled: bool = True

    _KEYS = (
        "id", "kind", "ref_id", "path", "label", "media_type", "version_policy",
        "pinned_version", "retrieval_policy", "role", "tags", "summary",
        "summary_revision", "created_by", "created_from_session_id",
        "created_from_run_id", "created_at", "updated_at", "content_revision",
        "index_status", "index_revision", "access_mode", "enabled",
    )

    @classmethod
    def parse(cls, raw: Any, path: str = "link") -> "ProjectContextLink":
        with _own_errors():
            data = as_mapping(raw, path)
            reject_unknown(data, cls._KEYS, path)
            kind = one_of(data, "kind", path, choices=LINK_KINDS)
            ref_id = text(data, "ref_id", path, required=False, max_len=256)
            ref_path = text(data, "path", path, required=False, max_len=4096)
            if kind in PATH_KINDS and not ref_path:
                raise ProjectContextError(f"{path}.path", f"is required for kind {kind!r}")
            if kind in REF_KINDS and not ref_id:
                raise ProjectContextError(f"{path}.ref_id", f"is required for kind {kind!r}")
            version_policy = one_of(data, "version_policy", path,
                                    choices=VERSION_POLICIES, required=False, default="latest")
            pinned = whole(data, "pinned_version", path, minimum=1)
            if version_policy == "pinned" and pinned is None:
                raise ProjectContextError(
                    f"{path}.pinned_version",
                    "is required when version_policy is 'pinned' (a pinned link with no "
                    "version silently follows the latest one, which is what pinning is for)",
                )
            return cls(
                id=text(data, "id", path, max_len=128),
                kind=kind, ref_id=ref_id, path=ref_path,
                label=text(data, "label", path, required=False, max_len=512),
                media_type=text(data, "media_type", path, required=False, max_len=128),
                version_policy=version_policy, pinned_version=pinned,
                retrieval_policy=one_of(data, "retrieval_policy", path,
                                        choices=RETRIEVAL_POLICIES, required=False,
                                        default="auto"),
                role=one_of(data, "role", path, choices=ROLES, required=False,
                            default="reference"),
                tags=text_list(data, "tags", path, max_items=64, max_len=64),
                summary=text(data, "summary", path, required=False, max_len=8000),
                summary_revision=text(data, "summary_revision", path, required=False,
                                      max_len=512),
                created_by=text(data, "created_by", path, required=False, max_len=128),
                created_from_session_id=text(data, "created_from_session_id", path,
                                             required=False, max_len=128),
                created_from_run_id=text(data, "created_from_run_id", path,
                                         required=False, max_len=128),
                created_at=whole(data, "created_at", path, default=0, minimum=0),
                updated_at=whole(data, "updated_at", path, default=0, minimum=0),
                content_revision=text(data, "content_revision", path, required=False,
                                      max_len=512),
                index_status=one_of(data, "index_status", path, choices=INDEX_STATUSES,
                                    required=False, default="none"),
                index_revision=text(data, "index_revision", path, required=False,
                                    max_len=512),
                access_mode=one_of(data, "access_mode", path, choices=ACCESS_MODES,
                                   required=False, default="read_only"),
                enabled=flag(data, "enabled", path, default=True),
            )

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(kind=self.kind, id=self.ref_id, path=self.path)

    @property
    def locator(self) -> str:
        return self.path if self.kind in PATH_KINDS else self.ref_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "ref_id": self.ref_id, "path": self.path,
            "label": self.label, "media_type": self.media_type,
            "version_policy": self.version_policy, "pinned_version": self.pinned_version,
            "retrieval_policy": self.retrieval_policy, "role": self.role,
            "tags": list(self.tags), "summary": self.summary,
            "summary_revision": self.summary_revision, "created_by": self.created_by,
            "created_from_session_id": self.created_from_session_id,
            "created_from_run_id": self.created_from_run_id,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "content_revision": self.content_revision, "index_status": self.index_status,
            "index_revision": self.index_revision, "access_mode": self.access_mode,
            "enabled": self.enabled,
        }


# ── what the service answers with ──────────────────────────────────────────
#
# Every mutation answers with a result object rather than raising, because the
# agent's confirmation to the user is built from this value. "He añadido X al
# proyecto" must be a reading of ``ok`` and ``link``, not an assumption that
# the call returned at all.

@dataclass(frozen=True)
class AttachResult:
    """The outcome of an attach. ``deduplicated`` is the difference between
    "I linked it" and "it was already linked" — the same true statement told
    two different ways, and the user can tell them apart."""

    ok: bool = False
    action: str = ""
    link: Optional[ProjectContextLink] = None
    deduplicated: bool = False
    project_id: str = ""
    message: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "action": self.action, "deduplicated": self.deduplicated,
                "project_id": self.project_id, "message": self.message,
                "error": self.error,
                "link": self.link.to_dict() if self.link else None}


@dataclass(frozen=True)
class DetachResult:
    """Detach removes the link. It never removes the source, and the wording
    here is what the agent repeats back, so it says so."""

    ok: bool = False
    action: str = ""
    link_id: str = ""
    project_id: str = ""
    message: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "action": self.action, "link_id": self.link_id,
                "project_id": self.project_id, "message": self.message,
                "error": self.error}


@dataclass(frozen=True)
class RefreshResult:
    """A recomputed revision. ``previous_revision`` is kept beside the new one
    because the index that is about to be rebuilt is still serving the old
    one, and the swap needs both halves (plan §13, double revision)."""

    ok: bool = False
    link: Optional[ProjectContextLink] = None
    previous_revision: str = ""
    revision: str = ""
    changed: bool = False
    index_status: str = ""
    state: str = ""
    message: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "previous_revision": self.previous_revision,
                "revision": self.revision, "changed": self.changed,
                "index_status": self.index_status, "state": self.state,
                "message": self.message, "error": self.error,
                "link": self.link.to_dict() if self.link else None}


@dataclass(frozen=True)
class ContextLinkStatus:
    """What ``inspect`` answers: the stored link beside what the source says
    right now. ``stale`` is the disagreement between the two — the stored
    revision is a claim, and the resolver is the only authority on it."""

    link: Optional[ProjectContextLink] = None
    metadata: SourceMetadata = field(default_factory=SourceMetadata)
    state: str = "ok"
    revision: str = ""
    effective_version: int = 0
    stale: bool = False
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "revision": self.revision,
                "effective_version": self.effective_version, "stale": self.stale,
                "message": self.message, "metadata": self.metadata.to_dict(),
                "link": self.link.to_dict() if self.link else None}
