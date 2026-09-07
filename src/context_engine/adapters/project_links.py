"""
context_engine/adapters/project_links.py — the manifest first, the content only
when the turn asks for it.

A project's typed context links (``src/project_context/``) are membership and
policy: *this document belongs to this project, at this revision, under this
retrieval policy*.  Turning that into context is two different jobs, and the
whole design of this module is refusing to confuse them.

**The manifest is what goes into every turn.**  One line per enabled link —
``- ctx_a1 [requirements, document, auto] Product requirements v4`` — in
``project_rules``, on the ``mandatory`` lane, carrying no byte of the source.
That is plan §12's "manifiesto ligero", and the reason it is a line and not a
document is arithmetic: six linked PDFs injected whole are a context window
spent before the question has been read.  A model that knows a requirements
document exists, what it is called and how to open it can ask for it; a model
handed forty thousand tokens of it cannot un-spend them.

**The content arrives only when the query asks for it.**  With a query, links
whose ``retrieval_policy`` is ``auto`` or ``pinned_summary`` are searched
through their own resolver and produce excerpts with a location.  ``on_demand``
and ``disabled`` do not participate — unless the caller named the link in
``explicit_refs``, which is the one channel by which a *person* overrides a
policy, and is deliberately not parsed out of the query text.

Four rules this module exists to keep, each of which is a failure someone
would otherwise ship:

**Isolation narrows the question, never the answer** (§12).  ``owner``,
``project_id`` and ``enabled`` go into ``ProjectStore.list_links`` as
arguments.  Fetching every project's links and dropping the foreign ones
afterwards would put another project's labels in this process's memory, one
``except`` clause away from a log line — and the labels are the part that
hurts, because "Q3 layoff plan" discloses the thing the check was protecting.

**Authorisation happens before the search, on this side of the call.**
``ContextSourceResolver.search()`` and ``.revision()`` take no ``owner``
(``resolvers/base.py`` says so, and says why: they must not become existence
oracles).  So this module calls ``resolver.metadata(..., owner=...)`` first and
searches only what came back ``ok``.  By the time a resolver has answered a
search, the answer is already here.

**An unindexed link is readable, and says so** (§12, immediate consistency).
``index_status="queued"`` does not make a link unusable: it is read directly
through its resolver, and every candidate that comes out of that read is
``degraded=True`` with a note.  "I read this file just now" and "this is in the
index" are different claims, and a packet that blurs them cannot be audited.

**Every candidate carries the whole provenance** (§15): ``link_id``, ``kind``,
``ref_id``, ``revision`` and location, in ``meta`` *and* encoded in the
``source_ref``, so "where did you get this and which version did you read?"
has an answer that survives the packet.

``source_ref`` schemes minted here:

    link:<link_id>                      the manifest entry for one link
    link:<link_id>#line=42              one located match inside it
    link:<link_id>#line=3&path=a%2Fb.md a match inside a linked folder

The fragment is ``urlencode``-d rather than joined by hand: a location can
carry a relative path, and a path can carry the separator somebody chose as a
delimiter.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate
from .objectives import resolve_project

logger = logging.getLogger(__name__)

#: The prefix :func:`candidates.fetch_ref` routes on.
REF_PREFIX = "link:"

#: Which section a linked source's content belongs in.  A linked file is part
#: of the map of the code the same way a workspace file is; a linked image is
#: read for its description, prompt and tags, which is what
#: ``multimodal_recipes`` is for.
SECTION_BY_KIND: Dict[str, str] = {
    "document": "retrieved_documents",
    "artifact": "retrieved_documents",
    "file": "code_map",
    "folder": "code_map",
    "gallery_image": "multimodal_recipes",
}

#: ``SOURCE_TYPES`` has no ``image``; a gallery image's searchable text *is*
#: its generation recipe (caption, prompt, tags), which is the closed
#: vocabulary's own word for it.
SOURCE_TYPE_BY_KIND: Dict[str, str] = {
    "document": "document",
    "artifact": "artifact",
    "file": "file",
    "folder": "file",
    "gallery_image": "recipe",
}

#: Policies a query may search without the link being named.  ``on_demand``
#: and ``disabled`` are absent on purpose — see the module docstring.
SEARCHABLE_POLICIES: Tuple[str, ...] = ("auto", "pinned_summary")

#: Statuses that mean "an index is pending, behind or broken".  A candidate
#: read while one of these holds is degraded: it was read directly, and saying
#: otherwise would claim an index that is not serving it.  ``none`` is not
#: here — a link nobody asked to index is not degraded by being read the only
#: way it can be.
PENDING_INDEX_STATUSES: Tuple[str, ...] = ("queued", "indexing", "stale", "failed")

#: Per link, per round.  Five hits from one document crowd out the other four
#: links, and `ranking.diversify` would drop them anyway.
MAX_MATCHES_PER_LINK = 3

#: A match snippet is one line; this is the clamp the resolvers already apply,
#: restated so a resolver that forgets cannot widen it here.
MAX_SNIPPET_CHARS = 400

#: `_fetch` reopens a located match with a few lines around it, not the file.
FETCH_CONTEXT_LINES = 4
MAX_FETCH_CHARS = 4_000


# ── the ref scheme ─────────────────────────────────────────────────────────

def link_ref(link_id: str, location: Optional[Mapping[str, Any]] = None) -> str:
    """``link:ctx_a1`` or ``link:ctx_a1#line=42&path=docs%2Fa.md``."""
    ref = f"{REF_PREFIX}{str(link_id or '').strip()}"
    pairs = [
        (str(key), str(value))
        for key, value in sorted((location or {}).items())
        if not isinstance(value, bool) and isinstance(value, (str, int, float))
        and str(value) != ""
    ]
    return f"{ref}#{urlencode(pairs)}" if pairs else ref


def parse_link_ref(source_ref: str) -> Tuple[str, Dict[str, str]]:
    """``("ctx_a1", {"line": "42"})``, or ``("", {})`` for anything else.

    Total and non-raising: it runs on the retrieval path, and a ref somebody
    hand-typed into ``explicit_refs`` must cost that ref, not the turn.
    """
    raw = str(source_ref or "").strip()
    if not raw.startswith(REF_PREFIX):
        return "", {}
    link_id, _, where = raw[len(REF_PREFIX):].partition("#")
    link_id = link_id.strip()
    if not link_id:
        return "", {}
    try:
        return link_id, dict(parse_qsl(where, keep_blank_values=False))
    except ValueError:                                          # malformed %XX
        return link_id, {}


def manifest_line(link: Mapping[str, Any]) -> str:
    """One link as the one line §12 specifies, and nothing else.

    Deliberately built from stored link fields only: id, role, kind, policy,
    label and — when it is not ``ready`` — the index state.  The link's stored
    ``summary`` is *not* in it even though §12 allows one, because "the
    manifest contains no content read from the source" is an invariant a test
    can state in one sentence, and a summary makes it a sentence with an
    exception.  It travels in ``meta`` instead, where a consumer that wants it
    can have it and a reader counting tokens can see what it cost.
    """
    label = str(link.get("label") or link.get("path") or link.get("ref_id") or "").strip()
    line = (f"- {link.get('id') or '?'} "
            f"[{link.get('role') or 'reference'}, "
            f"{link.get('kind') or '?'}, "
            f"{link.get('retrieval_policy') or 'auto'}] {label}").rstrip()
    status = str(link.get("index_status") or "none")
    if status in PENDING_INDEX_STATUSES:
        line = f"{line} (index: {status})"
    return line


def _where(location: Mapping[str, Any]) -> str:
    """`" (docs/a.md line 3)"` — the location as a person would read it."""
    parts: List[str] = []
    path = str(location.get("path") or "")
    if path:
        parts.append(path)
    version = location.get("version")
    if version not in (None, "", 0):
        parts.append(f"v{version}")
    line = location.get("line")
    if line not in (None, "", 0):
        parts.append(f"line {line}")
    return f" ({', '.join(parts)})" if parts else ""


def _window_around(text: str, line: Any) -> str:
    """A few lines of `text` around `line`, or its head when there is no line."""
    body = str(text or "")
    if not body:
        return ""
    try:
        number = int(line)
    except (TypeError, ValueError):
        return body[:MAX_FETCH_CHARS]
    lines = body.splitlines()
    if number <= 0 or not lines:
        return body[:MAX_FETCH_CHARS]
    start = max(0, number - 1 - FETCH_CONTEXT_LINES)
    end = min(len(lines), number + FETCH_CONTEXT_LINES)
    return "\n".join(lines[start:end])[:MAX_FETCH_CHARS]


# ── the source ─────────────────────────────────────────────────────────────

class ProjectLinksSource(ThreadedSource):
    """``src/project_context/`` — a project's typed knowledge sources."""

    source_id = "project_links"
    sections = ("project_rules", "retrieved_documents", "code_map",
                "multimodal_recipes")
    handles = (REF_PREFIX,)

    def available(self) -> bool:
        try:
            import services.projects            # noqa: F401
            import src.project_context.resolvers  # noqa: F401
        except Exception as exc:                                # noqa: BLE001
            logger.debug("project context links unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        """A project's links are project data by construction.

        ``planner.PROJECT_SOURCE_IDS`` is pinned to the three ids whose rows
        ``ranking.PROJECT_SOURCE_TYPES`` would refuse anyway, and this source
        produces ``document`` rows the ranker would let through — so the flag
        is enforced *here*, before the store is consulted, rather than by
        widening a list whose contract is "exactly what the ranker refuses".
        """
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not any(req.wants(section) for section in self.sections):
            return "no section this source fills was requested"
        return ""

    # ── isolation ──────────────────────────────────────────────────────────

    def _links(self, req: RetrievalRequest) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """``(project, enabled links)``, narrowed inside the store's query.

        ``owner``, ``project_id`` and ``enabled`` are arguments to
        ``list_links``, not a filter applied to a global answer (§12).  A
        project record with no ``id`` — the bare ``{"workspace": ...}``
        ``resolve_project`` falls back to — owns no links and is answered with
        an empty list rather than a guess.
        """
        project = resolve_project(req) or {}
        project_id = str(project.get("id") or "").strip()
        if not project_id:
            return dict(project), []
        try:
            from services.projects import get_store
            rows = get_store().list_links(
                project_id, owner=str(req.owner or "") or None, enabled_only=True)
        except Exception as exc:                                # noqa: BLE001
            logger.debug("project links unreadable for %s: %s", project_id, exc)
            return dict(project), []
        return dict(project), [dict(row) for row in (rows or ()) if isinstance(row, Mapping)]

    @staticmethod
    def _provenance(link: Mapping[str, Any]) -> Dict[str, Any]:
        """§15: everything needed to answer "where did this come from, and
        which revision did you read?" without reopening the link."""
        return {
            "link_id": str(link.get("id") or ""),
            "kind": str(link.get("kind") or ""),
            "ref_id": str(link.get("ref_id") or ""),
            "path": str(link.get("path") or ""),
            "label": str(link.get("label") or ""),
            "role": str(link.get("role") or ""),
            "retrieval_policy": str(link.get("retrieval_policy") or ""),
            "version_policy": str(link.get("version_policy") or ""),
            "pinned_version": link.get("pinned_version"),
            "revision": str(link.get("content_revision") or ""),
            "index_status": str(link.get("index_status") or ""),
            "access_mode": str(link.get("access_mode") or ""),
        }

    # ── search ─────────────────────────────────────────────────────────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        project, links = self._links(req)
        if not links:
            return ()

        limit = req.top()
        query = str(req.query or "").strip()
        explicit = {parse_link_ref(ref)[0] for ref in req.refs_for(REF_PREFIX)}
        explicit.discard("")

        # `disabled` means "never enters a prompt" (models.RETRIEVAL_POLICIES),
        # so it is absent from the manifest too, not merely unsearchable.
        listed = [ln for ln in links
                  if str(ln.get("retrieval_policy") or "") != "disabled"]

        out: List[ContextCandidate] = []
        if req.wants("project_rules") and listed:
            # With a query, half the round's allowance is held back for the
            # answer: a turn that asked a question wants one, not only a
            # catalogue.  With no query the catalogue *is* the answer.
            cap = limit if not query else max(1, (limit + 1) // 2)
            shown, unlisted = listed[:cap], max(0, len(listed) - cap)
            for link in shown:
                candidate = self._manifest_candidate(req, project, link,
                                                     unlisted=unlisted)
                if candidate is not None:
                    out.append(candidate)
            if unlisted:
                logger.debug("project link manifest truncated: %d of %d listed",
                             len(shown), len(listed))

        if query and req.allows("lexical"):
            out.extend(self._excerpts(req, project, links, query, explicit,
                                      max(0, limit - len(out))))
        return tuple(out)

    def _manifest_candidate(self, req: RetrievalRequest, project: Mapping[str, Any],
                            link: Mapping[str, Any], *,
                            lanes: Tuple[str, ...] = ("mandatory",),
                            unlisted: int = 0) -> Optional[ContextCandidate]:
        """One link as a pointer: id, label, policy, state.  No source bytes.

        ``meta["transformation"] = "reference"`` records what this *is*.
        ``transforms.fit()`` will label the item ``verbatim``, correctly — the
        line is verbatim what this adapter wrote — and the packet still says,
        through this key, that no content was transformed to produce it.
        """
        link_id = str(link.get("id") or "").strip()
        if not link_id:
            return None
        meta = self._provenance(link)
        meta.update({"manifest": True, "transformation": "reference"})
        summary = str(link.get("summary") or "").strip()
        if summary:
            meta["summary"] = summary[:MAX_SNIPPET_CHARS]
        if unlisted:
            meta["unlisted_links"] = int(unlisted)
        return make_candidate(
            source_type=SOURCE_TYPE_BY_KIND.get(str(link.get("kind") or ""), "document"),
            source_ref=link_ref(link_id),
            section="project_rules",
            title=f"{link.get('label') or link_id} (project knowledge source)",
            body=manifest_line(link),
            lanes=lanes,
            # The user attached this link; membership is their statement, and
            # `human_memory` is where §14.2 puts a durable one that is not an
            # instruction.
            trust_class="human_explicit",
            authority="human_memory",
            source_revision=str(link.get("content_revision") or ""),
            observed_at=link.get("updated_at") or link.get("created_at") or "",
            owner=req.owner,
            project_id=req.project_id or str(project.get("id") or ""),
            # A catalogue that had to be cut is a catalogue that no longer
            # names everything the project knows, and the packet says so.
            degraded=bool(unlisted),
            meta=meta,
        )

    # ── content, only for a query ──────────────────────────────────────────

    def _excerpts(self, req: RetrievalRequest, project: Mapping[str, Any],
                  links: Sequence[Mapping[str, Any]], query: str,
                  explicit: "set[str]", budget: int) -> List[ContextCandidate]:
        if budget <= 0:
            return []
        owner = str(req.owner or "")
        out: List[ContextCandidate] = []
        for link in links:
            if len(out) >= budget:
                break
            link_id = str(link.get("id") or "")
            named = link_id in explicit
            if not named and str(link.get("retrieval_policy") or "") not in SEARCHABLE_POLICIES:
                continue
            section = SECTION_BY_KIND.get(str(link.get("kind") or ""),
                                          "retrieved_documents")
            if not req.wants(section):
                continue
            wanted = min(MAX_MATCHES_PER_LINK, budget - len(out))
            for match in self._matches(link, query, owner, project, wanted):
                candidate = self._excerpt_candidate(req, project, link, match,
                                                    section, named=named)
                if candidate is not None:
                    out.append(candidate)
                if len(out) >= budget:
                    break
        return out

    @staticmethod
    def _matches(link: Mapping[str, Any], query: str, owner: str,
                 project: Mapping[str, Any], limit: int) -> List[Any]:
        """Owner-checked, *then* searched.

        ``search()`` takes no owner — the resolver protocol gives it none, so
        that it cannot be called on its own as an existence oracle.  That makes
        the check this module's job, and it has to happen before the call:
        after it, another owner's text is already in this process.
        """
        if limit <= 0:
            return []
        kind = str(link.get("kind") or "")
        try:
            from src.project_context.models import SourceRef
            from src.project_context.resolvers.base import get_resolver
        except Exception as exc:                                # noqa: BLE001
            logger.debug("project_context resolvers unavailable: %s", exc)
            return []
        resolver = get_resolver(kind)
        if resolver is None:
            logger.debug("no resolver registered for link kind %r", kind)
            return []
        ref = SourceRef(kind=kind, id=str(link.get("ref_id") or ""),
                        path=str(link.get("path") or ""))
        try:
            metadata = resolver.metadata(ref, owner=owner, project=project)
        except Exception as exc:                                # noqa: BLE001
            logger.debug("metadata for link %s failed: %s", link.get("id"), exc)
            return []
        if not metadata.ok:
            logger.debug("link %s is %s; not searched", link.get("id"), metadata.state)
            return []
        if metadata.owner and owner and metadata.owner != owner:
            # A resolver that answers `ok` for somebody else's source skipped
            # its own check; trusting it here would make this the module that
            # leaked. Same guard as `ProjectContextService._refusal`.
            logger.error("project_links: resolver returned an ok result owned by "
                         "somebody else; refusing to search it")
            return []

        link_id = str(link.get("id") or "")
        project_id = str(project.get("id") or "")
        expected_revision = str(link.get("content_revision") or "")
        index_revision = str(link.get("index_revision") or "")
        status = str(link.get("index_status") or "none")
        if status == "ready" and expected_revision and index_revision == expected_revision:
            try:
                from src.project_context import index as context_index
                durable_revision = context_index.indexed_revision(
                    owner=owner, project_id=project_id, link_id=link_id)
                if durable_revision == expected_revision:
                    indexed = context_index.search(
                        owner=owner, project_id=project_id, link_id=link_id,
                        query=query, limit=limit)
                    hits = [_RetrievedMatch.from_match(
                        hit, degraded=False, mode="index") for hit in indexed[:limit]]
                    if hits:
                        from src.project_context.service import note_retrieved
                        note_retrieved(
                            owner=owner, project_id=project_id, link_id=link_id,
                            source_kind=kind,
                            source_ref=str(link.get("ref_id") or link.get("path") or ""),
                            revision=expected_revision, mode="index", matches=len(hits))
                    return hits
            except Exception as exc:  # noqa: BLE001 - direct read remains available
                logger.debug("index search for link %s failed: %s", link_id, exc)
        try:
            direct = list(resolver.search(ref, query, limit=limit) or [])
        except Exception as exc:                                # noqa: BLE001
            logger.debug("search inside link %s failed: %s", link.get("id"), exc)
            return []
        # A ready marker without its matching durable index is degraded too:
        # serving a direct read is preferable to dropping the source, but it
        # must not be described as an indexed result.
        degraded = status in PENDING_INDEX_STATUSES or status == "ready"
        hits = [_RetrievedMatch.from_match(
            hit, degraded=degraded, mode="direct") for hit in direct[:limit]]
        if hits:
            try:
                from src.project_context.service import note_retrieved
                note_retrieved(
                    owner=owner, project_id=project_id, link_id=link_id,
                    source_kind=kind,
                    source_ref=str(link.get("ref_id") or link.get("path") or ""),
                    revision=str(getattr(hits[0], "revision", "") or expected_revision),
                    mode="direct", matches=len(hits))
            except Exception as exc:  # noqa: BLE001 - telemetry cannot break retrieval
                logger.debug("could not emit retrieval event for %s: %s", link_id, exc)
        return hits

    def _excerpt_candidate(self, req: RetrievalRequest, project: Mapping[str, Any],
                           link: Mapping[str, Any], match: Any, section: str, *,
                           named: bool,
                           lanes: Tuple[str, ...] = (),
                           max_body: int = MAX_SNIPPET_CHARS) -> Optional[ContextCandidate]:
        link_id = str(link.get("id") or "").strip()
        if not link_id:
            return None
        location = dict(getattr(match, "location", None) or {})
        revision = (str(getattr(match, "revision", "") or "")
                    or str(link.get("content_revision") or ""))
        status = str(link.get("index_status") or "none")
        degraded = bool(getattr(match, "degraded", status in PENDING_INDEX_STATUSES))

        meta = self._provenance(link)
        meta["location"] = location
        meta["revision"] = revision
        meta["retrieval_reason"] = "explicit_reference" if named else "keyword+role"
        meta["retrieval_mode"] = str(getattr(match, "mode", "direct") or "direct")
        if degraded:
            # §12: usable now, and honest about how. Never "indexed".
            suffix = ("the durable index could not be verified"
                      if status == "ready" else f"this link's index is {status}")
            meta["note"] = (f"read directly through the {link.get('kind')} resolver; "
                            f"{suffix}")
        if not lanes:
            lanes = ("explicit", "lexical") if named else ("lexical",)
        return make_candidate(
            source_type=SOURCE_TYPE_BY_KIND.get(str(link.get("kind") or ""), "document"),
            source_ref=link_ref(link_id, location),
            section=section,
            title=f"{link.get('label') or link_id}{_where(location)}",
            body=str(getattr(match, "snippet", "") or "")[:max(1, int(max_body))],
            lanes=lanes,
            scores={"lexical": float(getattr(match, "score", 0.0) or 0.0)},
            # Read from the source a moment ago: an observation, whatever the
            # link record claims about it.
            trust_class="observed",
            authority="observed_state",
            source_revision=revision,
            observed_at=time.time(),
            owner=req.owner,
            project_id=req.project_id or str(project.get("id") or ""),
            degraded=degraded,
            meta=meta,
        )

    # ── reopening one ref ──────────────────────────────────────────────────

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        """Rule 4 of ``contracts.py``: a ``source_ref`` is only provenance if
        something can still resolve it.

        ``link:<id>`` answers with the manifest entry; ``link:<id>#<location>``
        re-reads the source around that location.  A location that cannot be
        read any more degrades to the manifest entry rather than to None: the
        link still exists and the caller is entitled to know that much.
        """
        link_id, location = parse_link_ref(source_ref)
        if not link_id:
            return None
        project, links = self._links(req)
        link = next((ln for ln in links if str(ln.get("id") or "") == link_id), None)
        if link is None:
            return None
        if not location:
            return self._manifest_candidate(req, project, link, lanes=("exact",))

        body, revision = self._read_at(link, location, str(req.owner or ""), project)
        if not body.strip():
            return self._manifest_candidate(req, project, link, lanes=("exact",))
        window = _Window(location=location, snippet=body, score=1.0, revision=revision)
        return self._excerpt_candidate(
            req, project, link, window,
            SECTION_BY_KIND.get(str(link.get("kind") or ""), "retrieved_documents"),
            named=True, lanes=("exact",), max_body=MAX_FETCH_CHARS)

    def _read_at(self, link: Mapping[str, Any], location: Mapping[str, Any],
                 owner: str, project: Mapping[str, Any]) -> Tuple[str, str]:
        """``(text, revision)`` around a location, or ``("", "")``."""
        kind = str(link.get("kind") or "")
        try:
            from src.project_context.models import SourceRef
            from src.project_context.resolvers.base import get_resolver
        except Exception as exc:                                # noqa: BLE001
            logger.debug("project_context resolvers unavailable: %s", exc)
            return "", ""
        resolver = get_resolver(kind)
        if resolver is None:
            return "", ""
        ref = SourceRef(kind=kind, id=str(link.get("ref_id") or ""),
                        path=str(link.get("path") or ""))
        try:
            metadata = resolver.metadata(ref, owner=owner, project=project)
        except Exception as exc:                                # noqa: BLE001
            logger.debug("metadata for link %s failed: %s", link.get("id"), exc)
            return "", ""
        if not metadata.ok:
            return "", ""

        inner = str(location.get("path") or "")
        if kind == "folder" and inner:
            # A folder's own `read()` answers with its listing, and the match
            # was inside one of its files. The child is resolved through the
            # `file` resolver, which re-vets it — and containment is checked
            # here as well, because a match location is data that travelled
            # through a source_ref.
            ref, resolver = self._child(link, inner, owner, project)
            if ref is None or resolver is None:
                return "", ""
        try:
            content = resolver.read(ref, start=0, limit=0)
        except Exception as exc:                                # noqa: BLE001
            logger.debug("read of link %s failed: %s", link.get("id"), exc)
            return "", ""
        text = str(getattr(content, "text", "") or "")
        revision = str(getattr(content, "revision", "") or "")
        return _window_around(text, location.get("line")), revision

    @staticmethod
    def _child(link: Mapping[str, Any], inner: str, owner: str,
               project: Mapping[str, Any]) -> Tuple[Any, Any]:
        """A file inside a linked folder, or ``(None, None)``."""
        try:
            from src.project_context.models import SourceRef
            from src.project_context.resolvers.base import get_resolver
        except Exception:                                       # noqa: BLE001
            return None, None
        root = str(link.get("path") or "")
        if not root:
            return None, None
        try:
            root = os.path.realpath(root)
            target = os.path.realpath(os.path.join(root, inner))
            if os.path.commonpath([target, root]) != root:
                return None, None
        except (OSError, ValueError):
            return None, None
        resolver = get_resolver("file")
        if resolver is None:
            return None, None
        child = SourceRef(kind="file", path=target)
        try:
            if not resolver.metadata(child, owner=owner, project=project).ok:
                return None, None
        except Exception:                                       # noqa: BLE001
            return None, None
        return child, resolver


class _Window:
    """A ``SourceMatch``-shaped value for ``_fetch``'s re-read.

    Deliberately not the real ``SourceMatch``: importing the contract here
    would put ``src.project_context.models`` on this module's import path for
    the benefit of one duck-typed struct, and every reader of it goes through
    ``getattr``.
    """

    __slots__ = ("location", "snippet", "score", "revision")

    def __init__(self, *, location: Mapping[str, Any], snippet: str,
                 score: float = 1.0, revision: str = "") -> None:
        self.location = dict(location or {})
        self.snippet = snippet
        self.score = score
        self.revision = revision


class _RetrievedMatch:
    """A resolver/index match carrying how it was obtained."""

    __slots__ = ("location", "snippet", "score", "revision", "degraded", "mode")

    def __init__(self, *, location: Mapping[str, Any], snippet: str,
                 score: float, revision: str, degraded: bool, mode: str) -> None:
        self.location = dict(location or {})
        self.snippet = str(snippet or "")
        self.score = float(score or 0.0)
        self.revision = str(revision or "")
        self.degraded = bool(degraded)
        self.mode = str(mode or "direct")

    @classmethod
    def from_match(cls, match: Any, *, degraded: bool,
                   mode: str) -> "_RetrievedMatch":
        return cls(location=getattr(match, "location", None) or {},
                   snippet=getattr(match, "snippet", ""),
                   score=getattr(match, "score", 0.0),
                   revision=getattr(match, "revision", ""),
                   degraded=degraded, mode=mode)


__all__ = [
    "MAX_MATCHES_PER_LINK", "PENDING_INDEX_STATUSES", "REF_PREFIX",
    "SEARCHABLE_POLICIES", "SECTION_BY_KIND", "SOURCE_TYPE_BY_KIND",
    "ProjectLinksSource", "link_ref", "manifest_line", "parse_link_ref",
]
