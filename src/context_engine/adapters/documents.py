"""
context_engine/adapters/documents.py — third-party prose, labelled as such.

``src/rag_manager.py`` is a thin shell over ``src/rag_vector.py``, which is a
Chroma collection of chunks from whatever the user pointed the indexer at:
PDFs, exports, downloaded manuals, a colleague's notes.  Almost none of it was
written by this system, none of it was verified by it, and a lot of it is
several years old.  So every candidate from here leaves with
``trust_class="untrusted"`` and ``authority="inference"`` — the floor of both
scales.  That is not a judgement about the documents; it is the statement that
when a document chunk and an observed file disagree, the file wins without
anyone having to write a rule about it.

Two numbers matter and both are borrowed rather than invented:

**The similarity floor is 0.35**, which is
``ChatProcessor.RAG_SIMILARITY_THRESHOLD`` in ``src/chat_processor.py``.  It is
restated here as a constant instead of imported because that module pulls
``src.search`` (web search) and ``src.youtube_handler`` at import time, and
dragging an HTTP stack onto the retrieval path to read one float would trade a
duplicated number for a second of import time on every turn.
``tests/test_context_engine_sources.py`` reads the real attribute and asserts
the two agree, which is the same arrangement ``budgets.py`` uses for
``IMAGE_BLOCK_TOKENS``.

**``k`` is the round's limit**, not a constant, because a mandatory pass and a
wide pass want different amounts of the same collection.

``source_ref`` scheme: ``doc:<metadata.source>#chunk<metadata.chunk_id>``,
where ``source`` is the absolute path the indexer walked and ``chunk_id`` is
the ordinal within that file — the two fields ``index_personal_documents``
writes, so the reference resolves back to a real place on disk.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Mapping, Optional, Sequence

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: Mirror of ``src.chat_processor.ChatProcessor.RAG_SIMILARITY_THRESHOLD``.
#: See the module docstring for why it is a copy; the test keeps it honest.
RAG_SIMILARITY_FLOOR = 0.35


class DocumentSource(ThreadedSource):
    """``src/rag_manager.py`` — the personal document collection."""

    source_id = "documents"
    sections = ("retrieved_documents",)
    handles = ("doc:",)

    def __init__(self, manager: Any = None) -> None:
        # Constructing a `RAGManager` opens a Chroma client and can take
        # hundreds of milliseconds; building the default source list must not.
        self._manager = manager
        self._lock = threading.Lock()

    def available(self) -> bool:
        if self._manager is not None:
            return True
        try:
            import src.rag_manager  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("rag_manager unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("retrieved_documents"):
            return "retrieved_documents not requested"
        if not str(req.query or "").strip():
            # A vector search with no query is a random sample of the corpus.
            return "no query"
        if not (req.allows("semantic") or req.allows("lexical")):
            return "neither the semantic nor the lexical lane is open"
        return ""

    def _store(self) -> Any:
        with self._lock:
            if self._manager is None:
                from src.rag_manager import RAGManager
                self._manager = RAGManager()
            return self._manager

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        store = self._store()
        owner = str(req.owner or "")
        limit = req.top()
        # `owner=""` would be falsy inside rag_vector and drop the `where`
        # filter, so an ownerless request must not reach the collection at all.
        if not owner:
            logger.debug("documents: refusing an unscoped search")
            return ()
        hits = list(store.search(str(req.query or ""), k=limit, owner=owner) or [])
        degraded = not bool(getattr(getattr(store, "vector_rag", None), "healthy", True))
        out: List[ContextCandidate] = []
        for hit in hits:
            if float(hit.get("similarity") or 0.0) < RAG_SIMILARITY_FLOOR:
                continue
            candidate = self._candidate(hit, req, owner, degraded)
            if candidate is not None:
                out.append(candidate)
        return tuple(out[:limit])

    def _candidate(self, hit: Mapping[str, Any], req: RetrievalRequest,
                   owner: str, degraded: bool) -> Optional[ContextCandidate]:
        meta = hit.get("metadata")
        meta = dict(meta) if isinstance(meta, Mapping) else {}
        source = str(meta.get("source") or "")
        if not source:
            # No path means no reference means, by rule 4, no candidate.
            return None
        chunk_id = meta.get("chunk_id", 0)
        filename = str(meta.get("filename") or source.replace("\\", "/").rsplit("/", 1)[-1])
        return make_candidate(
            source_type="document",
            source_ref=f"doc:{source}#chunk{chunk_id}",
            section="retrieved_documents",
            title=f"{filename} (chunk {chunk_id})",
            body=str(hit.get("document") or ""),
            # rag_vector fuses a vector score with a keyword overlap in one
            # number, so both lanes really did run; when the embedding lane is
            # down `search` falls back to keywords and reports it as degraded.
            lanes=("semantic", "lexical") if not degraded else ("lexical",),
            scores={
                "similarity": hit.get("similarity", 0.0),
                "vector_similarity": hit.get("vector_similarity", 0.0),
                "keyword_score": hit.get("keyword_score", 0.0),
            },
            trust_class="untrusted",
            authority="inference",
            source_revision=str(hit.get("id") or ""),
            owner=str(meta.get("owner") or owner),
            project_id=req.project_id,
            degraded=degraded,
            meta={"path": source, "filename": filename, "chunk_id": chunk_id,
                  "type": str(meta.get("type") or ""),
                  "embedding_lane": str(hit.get("embedding_lane") or "")},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        """Not supported, and saying so is better than faking it.

        ``RAGManager`` exposes no read-by-id: the only way back to a chunk is a
        similarity search that may not return it.  A ``fetch`` that quietly ran
        a search would hand the caller a *different* chunk under the requested
        reference, which is worse than None in exactly the way this subsystem
        exists to prevent.
        """
        return None


__all__ = ["DocumentSource", "RAG_SIMILARITY_FLOOR"]
