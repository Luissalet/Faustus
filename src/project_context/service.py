"""
project_context/service.py — attach, detach, update, inspect and refresh.

Where the ownership, deduplication and validation logic lives, so that the
dispatch branch in ``src/tool_execution.py`` only adapts arguments and results
(plan §23). A giant ``elif`` is not a place to decide who owns a document.

The rule that shapes the whole module
-------------------------------------
**The project arrives as an object the server already resolved. This service
never accepts a project id in text from a model.** Every public method takes
``project`` as the resolved mapping produced by
``services.projects.project_context_for_session(...)`` / ``project_for_session``,
and a ``str`` handed in where that mapping belongs is a hard error, not a
lookup. A model that can name the destination project can move one project's
documents into another, and no amount of validation downstream repairs that.

The other five that follow from it
----------------------------------
* ``attach`` **validates the source before it writes**. A source that does not
  exist fails with ``state="missing"`` and is *never* re-pointed at a different
  source with the same title.
* ``attach`` is **idempotent**: the second identical request returns the first
  link with ``deduplicated=True``.
* ``detach`` removes the link and **never the source**.
* ``refresh`` recomputes the revision and marks ``index_status="stale"`` when
  it moved — and leaves ``index_revision`` alone, because the old index is
  still the one serving reads until the new one is complete (§13, double
  revision). Deleting the live index first is how a source becomes
  unretrievable for the length of a reindex.
* Before any mutation the owner of the context, of the project, of the actor
  and of the source are compared, and a disagreement **fails closed** (§17).

Everything a resolver returns is **untrusted data**, even when another agent
produced it (§17). Nothing here puts source text into an instruction position;
callers wrap it as quoted context, and instructions found inside a source
change no permission, tool, project or policy.

Events
------
The names this subsystem needs — ``project_context_attached`` and friends — are
not in ``src/contracts/event.py::EVENT_NAMES``, and that file is not this
change's to edit. ``_emit`` therefore *tries* the real ``emit()`` first, so the
moment those names are added every event routes for free, and until then it
records the envelope in a bounded in-process buffer (``unrouted_events()``) and
logs it. Nothing silently vanishes and nothing is smuggled out under a name
that means something else.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import ContractError

from .models import (
    ActorRef, AttachResult, ContextLinkStatus, DetachResult, PATCHABLE_FIELDS,
    ProjectContextError, ProjectContextLink, RefreshResult, SourceMetadata, SourceRef,
)
from .resolvers.base import get_resolver

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_EVENT_NAMES", "ProjectContextService", "clear_unrouted_events",
    "service", "unrouted_events",
]

#: The event names this subsystem emits. None of them is in ``EVENT_NAMES``
#: yet; see the module docstring. Listed here so whoever adds them has the set.
CONTEXT_EVENT_NAMES: Tuple[str, ...] = (
    "project_context_attached",
    "project_context_detached",
    "project_context_updated",
    "project_context_refresh_queued",
    "project_context_source_missing",
)

_UNROUTED_MAX = 200
_UNROUTED: List[Dict[str, Any]] = []
_UNROUTED_LOCK = threading.RLock()


def unrouted_events() -> List[Dict[str, Any]]:
    """Events whose name ``EVENT_NAMES`` does not know yet, newest last.

    An event nothing routes is a line in a log; keeping the envelopes means the
    day the names land, the gap is provable rather than remembered.
    """
    with _UNROUTED_LOCK:
        return [dict(e) for e in _UNROUTED]


def clear_unrouted_events() -> None:
    with _UNROUTED_LOCK:
        _UNROUTED.clear()


def _emit(name: str, **payload: Any) -> None:
    """Emit through the real envelope when the name is routable, buffer it when
    it is not. Never raises: an audit line must not be able to fail a mutation
    that already happened."""
    try:
        from src.contracts.event import emit as contract_emit
        contract_emit(name, **payload)
        return
    except ContractError:
        pass  # not in EVENT_NAMES — expected until those names are added
    except Exception as exc:  # noqa: BLE001
        logger.debug("project_context: emit(%s) failed: %s", name, exc)
        return
    envelope = {"name": name, "at": time.time(), **dict(payload)}
    with _UNROUTED_LOCK:
        _UNROUTED.append(envelope)
        if len(_UNROUTED) > _UNROUTED_MAX:
            del _UNROUTED[: len(_UNROUTED) - _UNROUTED_MAX]
    logger.info("project_context event %s (not yet in EVENT_NAMES): %s",
                name, {k: v for k, v in envelope.items() if k != "name"})


class ProjectContextService:
    """The one place a project's links are created, changed and read back.

    ``store`` is a ``services.projects.ProjectStore``-shaped object; left None
    it is fetched lazily so importing this module costs nothing.
    ``resolver_map`` overrides the global resolver registry (tests, and any
    future per-run restriction). ``clock`` returns epoch seconds.
    """

    def __init__(self, *, store=None, resolver_map=None, clock=None) -> None:
        self._store = store
        # ``{}`` means "no resolvers", not "use the global registry". Treating
        # an explicitly empty map as absent would silently reconnect a caller
        # that asked to be restricted to the process-wide set.
        self._resolver_map = dict(resolver_map) if resolver_map is not None else None
        self._clock = clock or time.time

    # ── plumbing ───────────────────────────────────────────────────────────

    def _now(self) -> int:
        return int(self._clock())

    def store(self):
        if self._store is not None:
            return self._store
        from services.projects import get_store
        return get_store()

    def _op(self, name: str):
        """A store method, or a rejection that says which one is missing.

        The link API is delivered by ``services/projects.py``; naming the exact
        method beats an ``AttributeError`` from three frames down.
        """
        store = self.store()
        fn = getattr(store, name, None)
        if not callable(fn):
            raise ProjectContextError(
                f"ProjectStore.{name}",
                "is not available; the project link API is not installed on this store",
            )
        return fn

    def _resolver(self, kind: str):
        if self._resolver_map is not None:
            return self._resolver_map.get(kind)
        return get_resolver(kind)

    @staticmethod
    def _project_id(project: Any) -> str:
        """The project's id — and the refusal that keeps rule 1 true.

        A string here means somebody passed an id where a resolved project
        belongs, which is exactly the shape of "the model chose the project".
        """
        if isinstance(project, str):
            raise ProjectContextError(
                "project",
                "must be the resolved project object, not a project id; this service "
                "never accepts a project chosen by a model (plan section 5.2)",
                got=project,
            )
        if not isinstance(project, Mapping):
            raise ProjectContextError("project", "must be a resolved project object",
                                      got=project)
        project_id = str(project.get("id") or "").strip()
        if not project_id:
            raise ProjectContextError("project.id", "is required")
        return project_id

    @staticmethod
    def _effective_owner(owner: Any) -> str:
        """The owner every check and every store call below uses.

        A blank owner is not automatically an attack. This project runs
        single-user by default — auth off, or the loopback bypass the local
        instance uses — and there `effective_storage_owner` resolves the blank
        to the one reserved local identity that `memory_engine`, the artifact
        store and the session table already scope by. Refusing it outright made
        this whole feature dead on exactly the installation it was written for
        while protecting nothing: with auth ON the helper hands the blank back
        unchanged and `_owner_mismatch` still refuses.

        Resolved once, at the top of each public method, so that the gate and
        the store query can never disagree about who is asking.
        """
        resolved = str(owner or "").strip()
        if resolved:
            return resolved
        try:
            from src.owner_identity import effective_storage_owner
            return str(effective_storage_owner(resolved) or "").strip()
        except Exception:  # pragma: no cover - import safety only
            return ""

    @staticmethod
    def _owner_mismatch(project: Mapping[str, Any], owner: str,
                        actor: Optional[ActorRef]) -> str:
        """The reason to refuse, or ``""``. Compares the context owner, the
        project's owner and the actor's, before anything is touched."""
        owner = str(owner or "").strip()
        if not owner:
            return "the request has no effective owner"
        if not project.get("enabled", True):
            return "the project is disabled"
        project_owner = str(project.get("owner") or "").strip()
        if project_owner and project_owner != owner:
            return "the project belongs to a different owner"
        if actor is not None and actor.kind == "user":
            actor_name = str(actor.name or "").strip()
            if actor_name and actor_name != owner:
                return "the acting user is not the owner of this request"
        return ""

    @staticmethod
    def _as_source(source: Any) -> SourceRef:
        return source if isinstance(source, SourceRef) else SourceRef.parse(source)

    @staticmethod
    def _as_actor(actor: Any) -> ActorRef:
        if isinstance(actor, ActorRef):
            return actor
        if actor is None:
            return ActorRef(kind="system")
        return ActorRef.parse(actor)

    @staticmethod
    def _parse_link(raw: Any) -> ProjectContextLink:
        """Parse a row the store handed back.

        Liberal on read, on purpose: the store owns legacy ``context_items``
        normalisation, and a row may still carry a key this contract does not
        model (``name``, from the pre-link shape). Those are dropped and logged
        rather than raised — a stored row is a peer module's output, not a
        manifest somebody is writing by hand.
        """
        data = dict(raw or {})
        extra = sorted(k for k in data if k not in ProjectContextLink._KEYS)
        if extra:
            logger.debug("project_context: ignoring unmodelled link fields %s", extra)
            data = {k: v for k, v in data.items() if k in ProjectContextLink._KEYS}
        return ProjectContextLink.parse(data)

    def _link_id(self) -> str:
        return "ctx_" + uuid.uuid4().hex[:10]

    def _event(self, name: str, *, project_id: str, owner: str, actor: ActorRef,
               link: Optional[ProjectContextLink] = None, link_id: str = "",
               **extra: Any) -> None:
        # Popped unconditionally: leaving them in ``extra`` when a link is also
        # given would pass the same keyword twice.
        kind = extra.pop("source_kind", "")
        locator = extra.pop("source_ref", "")
        _emit(
            name, owner=owner, project_id=project_id,
            session_id=actor.session_id, run_id=actor.run_id,
            link_id=link_id or (link.id if link else ""),
            source_kind=(link.kind if link else kind),
            source_ref=(link.locator if link else locator),
            actor=actor.kind, **extra,
        )

    # ── attach ─────────────────────────────────────────────────────────────

    def attach(self, *, project, owner: str, source, actor,
               retrieval_policy: str = "auto", version_policy: str = "latest",
               pinned_version: Optional[int] = None, role: str = "reference",
               label: str = "", tags: Optional[Sequence[str]] = None,
               access_mode: str = "read_only") -> AttachResult:
        """Link a source to a project, once.

        Validates the source *before* writing anything, is idempotent per
        ``(project, kind, canonical ref, version policy, pinned version)``, and
        never substitutes a different source that happens to share a title.
        """
        project_id = self._project_id(project)
        actor = self._as_actor(actor)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, actor)
        if deny:
            logger.warning("project_context: refusing attach on %s: %s", project_id, deny)
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error="owner_mismatch", message=deny)
        try:
            ref = self._as_source(source)
        except ContractError as exc:
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error="invalid_source", message=str(exc))

        resolver = self._resolver(ref.kind)
        if resolver is None:
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error="unsupported_kind",
                                message=f"no resolver is registered for kind {ref.kind!r}")

        note = getattr(resolver, "policy_note", lambda _p: "")(version_policy)
        if note:
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error="unsupported", message=note)

        metadata = resolver.validate(ref, owner=owner, project=project)
        refusal = self._refusal(metadata, owner)
        if refusal:
            state, message = refusal
            if state == "missing":
                self._event("project_context_source_missing", project_id=project_id,
                            owner=owner, actor=actor, source_kind=ref.kind,
                            source_ref=ref.locator)
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error=state, message=message)

        revision = resolver.revision(ref, version_policy=version_policy,
                                     pinned_version=pinned_version)
        if not revision:
            # The source exists but the *effective revision* does not — a pinned
            # version that was never written, for instance. Not an excuse to
            # fall back to the latest one.
            return AttachResult(
                ok=False, action="attach", project_id=project_id, error="missing",
                message=("the requested revision of this source does not exist"
                         + (f" (pinned version {pinned_version})" if pinned_version else "")),
            )

        now = self._now()
        raw = {
            "id": self._link_id(), "kind": ref.kind,
            "ref_id": ref.id, "path": ref.path,
            "label": (label or metadata.label or ref.locator)[:512],
            "media_type": metadata.media_type,
            "version_policy": version_policy, "pinned_version": pinned_version,
            "retrieval_policy": retrieval_policy, "role": role,
            "tags": list(tags or ()), "summary": "", "summary_revision": "",
            "created_by": actor.kind,
            "created_from_session_id": actor.session_id,
            "created_from_run_id": actor.run_id,
            "created_at": now, "updated_at": now,
            "content_revision": revision,
            "index_status": ("none" if retrieval_policy == "disabled" else "queued"),
            "index_revision": "", "access_mode": access_mode, "enabled": True,
        }
        try:
            candidate = ProjectContextLink.parse(raw).to_dict()
        except ContractError as exc:
            return AttachResult(ok=False, action="attach", project_id=project_id,
                                error="invalid_link", message=str(exc))

        normalize = getattr(self.store(), "normalize_link", None)
        if callable(normalize):
            candidate = dict(normalize(candidate) or candidate)
        saved, deduplicated = self._op("upsert_link")(project_id, candidate, owner=owner)
        link = self._parse_link(saved)
        self._event("project_context_attached", project_id=project_id, owner=owner,
                    actor=actor, link=link, deduplicated=bool(deduplicated),
                    revision=link.content_revision)
        return AttachResult(
            ok=True, action=("deduplicated" if deduplicated else "attached"),
            link=link, deduplicated=bool(deduplicated), project_id=project_id,
            message=(f"{link.label!r} was already part of this project's context"
                     if deduplicated else
                     f"{link.label!r} is now part of this project's context"),
        )

    @staticmethod
    def _refusal(metadata: SourceMetadata, owner: str) -> "Optional[Tuple[str, str]]":
        """``(state, message)`` when a resolver's answer must stop a mutation.

        The second check is defence against a resolver bug rather than against
        the user: an ``ok`` that names a different owner is a resolver that
        skipped its own check, and trusting it here would make this service the
        one that leaked.
        """
        if not metadata.ok:
            default = {"missing": "the source does not exist",
                       "forbidden": "the source is not available to this owner",
                       "unsupported": "this source kind cannot be linked yet"}
            return metadata.state, (metadata.note or default.get(metadata.state, "refused"))
        if metadata.owner and metadata.owner != owner:
            logger.error("project_context: resolver returned an ok result owned by "
                         "somebody else; refusing")
            return "forbidden", "the source is not available to this owner"
        return None

    # ── detach ─────────────────────────────────────────────────────────────

    def detach(self, *, project, owner: str, link_id: str, actor) -> DetachResult:
        """Remove the link. **The source is never touched** — the document, the
        artifact and the file on disk all survive, and the message says so,
        because the agent repeats that message to the user."""
        project_id = self._project_id(project)
        actor = self._as_actor(actor)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, actor)
        if deny:
            logger.warning("project_context: refusing detach on %s: %s", project_id, deny)
            return DetachResult(ok=False, action="detach", link_id=str(link_id or ""),
                                project_id=project_id, error="owner_mismatch", message=deny)
        link_id = str(link_id or "").strip()
        existing = self._op("get_link")(project_id, link_id, owner=owner)
        if not existing:
            return DetachResult(ok=False, action="detach", link_id=link_id,
                                project_id=project_id, error="missing",
                                message="no such link in this project")
        link = self._parse_link(existing)
        removed = bool(self._op("remove_link")(project_id, link_id, owner=owner))
        if not removed:
            return DetachResult(ok=False, action="detach", link_id=link_id,
                                project_id=project_id, error="missing",
                                message="no such link in this project")
        self._event("project_context_detached", project_id=project_id, owner=owner,
                    actor=actor, link=link)
        return DetachResult(
            ok=True, action="detached", link_id=link_id, project_id=project_id,
            message=(f"{link.label!r} is no longer part of this project's context. "
                     "The source itself was not deleted."),
        )

    # ── update ─────────────────────────────────────────────────────────────

    def update(self, *, project, owner: str, link_id: str, patch: Mapping[str, Any],
               actor) -> ProjectContextLink:
        """Change policy and metadata on an existing link.

        Only ``PATCHABLE_FIELDS``. Identity, provenance and revision are not
        patchable: an update that could rewrite ``ref_id`` would repoint an
        approved link at a different source, which is the attack ``attach``'s
        validation exists to prevent, arriving through the back door.

        Returns the link, so it raises rather than reporting — the caller has a
        value to hand back and no envelope to put a failure in.
        """
        project_id = self._project_id(project)
        actor = self._as_actor(actor)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, actor)
        if deny:
            raise ProjectContextError("owner", f"cannot update this project: {deny}")
        link_id = str(link_id or "").strip()
        patch = dict(patch or {})
        unknown = sorted(k for k in patch if k not in PATCHABLE_FIELDS)
        if unknown:
            raise ProjectContextError(
                "patch", f"cannot change {unknown}; updatable fields are "
                         f"{list(PATCHABLE_FIELDS)}")
        existing = self._op("get_link")(project_id, link_id, owner=owner)
        if not existing:
            raise ProjectContextError("link_id", "not found in this project", got=link_id)
        link = self._parse_link(existing)

        # A policy change moves the effective revision, so the source is
        # re-validated and the revision recomputed before it is written.
        if "version_policy" in patch or "pinned_version" in patch:
            version_policy = str(patch.get("version_policy", link.version_policy))
            pinned = patch.get("pinned_version", link.pinned_version)
            resolver = self._resolver(link.kind)
            if resolver is None:
                raise ProjectContextError("link.kind", "has no registered resolver",
                                          got=link.kind)
            note = getattr(resolver, "policy_note", lambda _p: "")(version_policy)
            if note:
                raise ProjectContextError("patch.version_policy", note)
            metadata = resolver.validate(link.source_ref, owner=owner, project=project)
            refusal = self._refusal(metadata, owner)
            if refusal:
                raise ProjectContextError("link", f"cannot be updated: {refusal[1]}")
            revision = resolver.revision(link.source_ref, version_policy=version_policy,
                                         pinned_version=pinned)
            if not revision:
                raise ProjectContextError(
                    "patch.pinned_version",
                    "that revision of the source does not exist", got=pinned)
            patch["content_revision"] = revision
            if revision != link.content_revision:
                # Stale, not cleared: the previous index keeps serving until a
                # new one is complete (§13).
                patch["index_status"] = "stale"

        patch["updated_at"] = self._now()
        saved = self._op("patch_link")(project_id, link_id, patch, owner=owner)
        updated = self._parse_link(saved)
        self._event("project_context_updated", project_id=project_id, owner=owner,
                    actor=actor, link=updated,
                    fields=sorted(k for k in patch if k != "updated_at"))
        return updated

    # ── list ───────────────────────────────────────────────────────────────

    def list(self, *, project, owner: str, status: Optional[str] = None,
             kind: Optional[str] = None) -> List[ProjectContextLink]:
        """The project's links. An owner mismatch returns nothing and logs it:
        a read fails closed and silently rather than raising on the turn path
        or, worse, answering."""
        project_id = self._project_id(project)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, None)
        if deny:
            logger.warning("project_context: refusing list on %s: %s", project_id, deny)
            return []
        rows = self._op("list_links")(project_id, owner=owner, kind=(kind or ""))
        out: List[ProjectContextLink] = []
        for row in rows or ():
            try:
                link = self._parse_link(row)
            except ContractError as exc:
                # One unreadable row must not hide the rest of the project.
                logger.warning("project_context: skipping an unreadable link row: %s", exc)
                continue
            if status and link.index_status != status:
                continue
            out.append(link)
        return out

    # ── inspect ────────────────────────────────────────────────────────────

    def inspect(self, *, project, owner: str, link_id: str) -> ContextLinkStatus:
        """The stored link beside what the source says right now.

        ``stale`` is the disagreement between the two. The stored revision is a
        claim written at attach time; the resolver is the only authority on
        what the source is today.
        """
        project_id = self._project_id(project)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, None)
        if deny:
            return ContextLinkStatus(state="forbidden", message=deny)
        existing = self._op("get_link")(project_id, str(link_id or "").strip(), owner=owner)
        if not existing:
            return ContextLinkStatus(state="missing",
                                     message="no such link in this project")
        link = self._parse_link(existing)
        resolver = self._resolver(link.kind)
        if resolver is None:
            return ContextLinkStatus(link=link, state="unsupported",
                                     message=f"no resolver for kind {link.kind!r}")
        metadata = resolver.metadata(link.source_ref, owner=owner, project=project)
        if not metadata.ok:
            return ContextLinkStatus(link=link, metadata=metadata, state=metadata.state,
                                     message=(metadata.note or "the source is unavailable"))
        revision = resolver.revision(link.source_ref, version_policy=link.version_policy,
                                     pinned_version=link.pinned_version)
        stale = bool(revision) and revision != link.content_revision
        return ContextLinkStatus(
            link=link, metadata=metadata, state="ok", revision=revision,
            effective_version=(link.pinned_version or metadata.version or 0),
            stale=stale,
            message=("the source has changed since this link was last refreshed"
                     if stale else "the link matches the source"),
        )

    # ── refresh ────────────────────────────────────────────────────────────

    def refresh(self, *, project, owner: str, link_id: str, actor) -> RefreshResult:
        """Recompute the revision and queue a reindex when it moved.

        The old ``index_revision`` is **kept**. Marking a link stale says "the
        index is behind"; clearing the revision would say "there is no index",
        and for the length of the rebuild the source would be unretrievable
        even though a perfectly good index is still sitting there (§13).
        """
        project_id = self._project_id(project)
        actor = self._as_actor(actor)
        owner = self._effective_owner(owner)
        deny = self._owner_mismatch(project, owner, actor)
        if deny:
            logger.warning("project_context: refusing refresh on %s: %s", project_id, deny)
            return RefreshResult(ok=False, error="owner_mismatch", message=deny)
        link_id = str(link_id or "").strip()
        existing = self._op("get_link")(project_id, link_id, owner=owner)
        if not existing:
            return RefreshResult(ok=False, error="missing",
                                 message="no such link in this project")
        link = self._parse_link(existing)
        resolver = self._resolver(link.kind)
        if resolver is None:
            return RefreshResult(ok=False, link=link, error="unsupported_kind",
                                 message=f"no resolver for kind {link.kind!r}")

        metadata = resolver.metadata(link.source_ref, owner=owner, project=project)
        refusal = self._refusal(metadata, owner)
        if refusal:
            state, message = refusal
            if state == "missing":
                # The link stays, and stays detachable: a broken link is
                # evidence, and hunting for another source with the same title
                # is exactly what §19 forbids.
                self._event("project_context_source_missing", project_id=project_id,
                            owner=owner, actor=actor, link=link)
            return RefreshResult(ok=False, link=link, state=state, error=state,
                                 previous_revision=link.content_revision,
                                 index_status=link.index_status, message=message)

        revision = resolver.revision(link.source_ref, version_policy=link.version_policy,
                                     pinned_version=link.pinned_version)
        if not revision:
            return RefreshResult(ok=False, link=link, state="missing", error="missing",
                                 previous_revision=link.content_revision,
                                 index_status=link.index_status,
                                 message="the requested revision no longer exists")
        if revision == link.content_revision:
            return RefreshResult(ok=True, link=link, state="ok", changed=False,
                                 previous_revision=link.content_revision,
                                 revision=revision, index_status=link.index_status,
                                 message="the source has not changed")

        # index_revision is deliberately absent from this patch: the old index
        # keeps serving until the new one is complete.
        patch = {"content_revision": revision, "index_status": "stale",
                 "updated_at": self._now()}
        saved = self._op("patch_link")(project_id, link_id, patch, owner=owner)
        updated = self._parse_link(saved)
        self._event("project_context_refresh_queued", project_id=project_id, owner=owner,
                    actor=actor, link=updated, previous_revision=link.content_revision,
                    revision=revision)
        return RefreshResult(
            ok=True, link=updated, state="ok", changed=True,
            previous_revision=link.content_revision, revision=revision,
            index_status=updated.index_status,
            message="the source changed; its index is stale and a rebuild is queued",
        )


# ── the process singleton ──────────────────────────────────────────────────

_SERVICE: Optional[ProjectContextService] = None
_SERVICE_LOCK = threading.RLock()


def service() -> ProjectContextService:
    """The shared service. Holds no state of its own beyond its collaborators,
    so one per process is a convenience, not a constraint."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ProjectContextService()
        return _SERVICE
