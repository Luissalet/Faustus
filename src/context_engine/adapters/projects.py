"""
context_engine/adapters/projects.py — the project's own words, one file at a time.

``<workspace>/.odysseus/`` is where a project writes down what it expects of
anyone working in it: ``MEMORY.md`` as the index, one markdown file per topic
beside it, and the project's standing instructions stored on the record itself.
All of it is ``project_rules``, all of it is ``human_explicit``, and none of it
was found by a search — a rule does not become less binding because the current
question does not mention it.

The decision that shapes this module: **one candidate per file, never a dump.**
A single blob of every ``.md`` in ``.odysseus/`` is one item the budget can
only take or leave whole, one ``source_ref`` for six different documents, and
one receipt that cannot say which of them the model actually used.  Per file,
the compiler can drop the stale one, the manifest names the file that produced
each sentence, and a user reading the packet can open it.

Ranking, when a query exists, is BM25 over the file bodies — borrowed from
``memory_engine.bm25_scores`` rather than rewritten, because a second, subtly
different lexical scorer in the same process is a bug waiting for a bug report
nobody can reproduce.  With no query the files come back in the store's own
order (index first, then alphabetical) on the ``mandatory`` lane, since there
is nothing to rank against and the index is the file a reader would open first.

``source_ref`` schemes:

    project:instructions        the project's standing instructions block
    project:<filename.md>       one file under <workspace>/.odysseus/
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate
from .objectives import resolve_project

logger = logging.getLogger(__name__)

#: A project memory file is meant to be read by a person; past this size it is
#: a data file that wandered into the wrong directory.  Clipping keeps one
#: runaway export from eating a section's whole budget before ranking runs.
MAX_FILE_CHARS = 20_000


def _bm25(query: str, docs: Sequence[Tuple[str, str]]) -> Dict[str, float]:
    """The house lexical scorer, or an empty ranking.

    Imported lazily and failing to "no ranking" rather than to a home-grown
    fallback: two scorers that disagree are worse than one scorer that is
    sometimes absent, and absence here only costs the ordering of a handful of
    files.
    """
    try:
        from src.memory_engine import bm25_scores
        return dict(bm25_scores(query, list(docs)))
    except Exception as exc:                                   # noqa: BLE001
        logger.debug("bm25 ranking unavailable for project memory: %s", exc)
        return {}


class ProjectMemorySource(ThreadedSource):
    """``services/projects.py`` — MEMORY.md, its siblings, and the instructions."""

    source_id = "project_memory"
    sections = ("project_rules",)
    handles = ("project:",)

    def available(self) -> bool:
        try:
            import services.projects  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("services.projects unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.wants("project_rules"):
            return "project_rules not requested"
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from services.projects import get_store, instructions_for_session

        project = resolve_project(req)
        if not project:
            return ()
        limit = req.top()
        out: List[ContextCandidate] = []

        if req.session_id:
            try:
                block = instructions_for_session(req.session_id, str(req.owner or "") or None)
            except Exception as exc:                           # noqa: BLE001
                logger.debug("project instructions unavailable: %s", exc)
                block = ""
            if block.strip():
                candidate = make_candidate(
                    source_type="instruction",
                    source_ref="project:instructions",
                    section="project_rules",
                    title=f"project instructions ({project.get('name') or 'project'})",
                    body=block,
                    lanes=("mandatory",),
                    # The user wrote these on the project record and every turn
                    # in the project is bound by them: the highest authority
                    # §14.2 knows, above even an observed file.
                    trust_class="human_explicit",
                    authority="user_instruction",
                    owner=req.owner,
                    project_id=req.project_id,
                    meta={"project": str(project.get("name") or "")},
                )
                if candidate is not None:
                    out.append(candidate)

        out.extend(self._files(get_store(), project, req, limit))
        return tuple(out)

    def _files(self, store: Any, project: Dict[str, Any], req: RetrievalRequest,
               limit: int) -> List[ContextCandidate]:
        try:
            listing = list(store.list_memory_files(project) or [])
        except Exception as exc:                               # noqa: BLE001
            logger.debug("project memory listing failed: %s", exc)
            return []
        if not listing:
            return []

        from services.projects import MEMORY_INDEX

        bodies: Dict[str, str] = {}
        for entry in listing:
            name = str(entry.get("name") or "")
            if not name:
                continue
            try:
                # The index has its own accessor; the rest go through
                # `read_memory_file`, which raises ProjectError on an unreadable
                # file and returns "" for a missing one.  Both mean "skip this
                # file", and neither may reach the caller as an exception.
                text = (store.read_index(project) if name == MEMORY_INDEX
                        else store.read_memory_file(project, name))
            except Exception as exc:                           # noqa: BLE001
                logger.debug("project memory file %s unreadable: %s", name, exc)
                continue
            if text and text.strip():
                bodies[name] = text[:MAX_FILE_CHARS]

        query = str(req.query or "").strip()
        if query and req.allows("lexical"):
            scores = _bm25(query, [(n, b) for n, b in bodies.items()])
            lanes: Tuple[str, ...] = ("lexical",)
            order = sorted(bodies, key=lambda n: (-scores.get(n, 0.0), n))
        else:
            scores = {}
            lanes = ("mandatory",)
            order = [str(e.get("name") or "") for e in listing if e.get("name") in bodies]

        by_name = {str(e.get("name") or ""): e for e in listing}
        out: List[ContextCandidate] = []
        for name in order[:limit]:
            entry = by_name.get(name) or {}
            candidate = make_candidate(
                source_type="project_memory",
                source_ref=f"project:{name}",
                section="project_rules",
                title=f"{name} (project memory)",
                body=bodies[name],
                lanes=lanes,
                scores={"lexical": scores.get(name, 0.0)} if scores else {},
                trust_class="human_explicit",
                authority="human_memory",
                # size:mtime is the cheapest revision that changes whenever the
                # file does, and it is what a later staleness check can compare
                # without re-reading the body.
                source_revision=f"{entry.get('size', 0)}:{entry.get('modified', 0)}",
                observed_at=entry.get("modified") or "",
                owner=req.owner,
                project_id=req.project_id,
                meta={"filename": name,
                      "path": os.path.join(store.memory_dir(project), name)},
            )
            if candidate is not None:
                out.append(candidate)
        return out

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from services.projects import get_store, instructions_for_session

        ref = str(source_ref or "")
        if not ref.startswith("project:"):
            return None
        name = ref[8:]
        project = resolve_project(req)
        if not project:
            return None
        if name == "instructions":
            try:
                block = instructions_for_session(req.session_id,
                                                 str(req.owner or "") or None)
            except Exception as exc:                           # noqa: BLE001
                logger.debug("project instructions unavailable: %s", exc)
                return None
            if not block.strip():
                return None
            return make_candidate(
                source_type="instruction", source_ref=ref, section="project_rules",
                title="project instructions", body=block, lanes=("exact",),
                trust_class="human_explicit", authority="user_instruction",
                owner=req.owner, project_id=req.project_id)
        store = get_store()
        try:
            text = store.read_memory_file(project, name)
        except Exception as exc:                               # noqa: BLE001
            logger.debug("project memory file %s unreadable: %s", name, exc)
            return None
        if not text or not text.strip():
            return None
        return make_candidate(
            source_type="project_memory", source_ref=ref, section="project_rules",
            title=f"{name} (project memory)", body=text[:MAX_FILE_CHARS],
            lanes=("exact",), trust_class="human_explicit", authority="human_memory",
            owner=req.owner, project_id=req.project_id, meta={"filename": name})


__all__ = ["ProjectMemorySource", "MAX_FILE_CHARS"]
