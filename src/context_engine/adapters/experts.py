"""
context_engine/adapters/experts.py — a corpus you have to ask for by name.

``services/experts.py`` holds one index per expert: a directory of books,
papers and manuals a user deliberately put there, searched with BM25 fused
with embeddings and optionally reordered by a cross-encoder.  It is the most
expensive source in this package and the least universally relevant, so unlike
every other adapter here it does **not** run on every turn.  It runs when the
request names a slug, and not otherwise.

The naming channel is ``explicit_refs`` with an ``expert:<slug>`` prefix, plus
``task.required_evidence`` with the same prefix.  Both come from the runtime.
The plan's original wording allowed "``req.request.task`` or the request's
``meta``"; there is no ``meta`` on ``ContextRequest``, and parsing a slug out
of the query text would mean a model could point the retrieval at any corpus on
the machine by writing its name in a sentence.  ``explicit_refs`` is the field
whose whole purpose is "the caller passed this in", and it is the one used.

Degradation is propagated in both of the ways ``search()`` can lose a lane:
its own ``degraded`` flag (the vector store is missing or raised) and a ``tier``
that came back ``lexical`` when a hybrid was expected.  A reranked hybrid is
not degraded; a lexical-only answer is, because the semantic half of the
retrieval did not happen and the caller needs to know before it trusts the
ranking.

Chunks are third-party text — someone else's book — so ``trust_class`` is
``untrusted`` and ``authority`` is ``inference``, the same floor as
``documents.py``.  The difference between the two sources is intent, not
reliability: an expert corpus was chosen for the question, a document
collection was merely indexed.

``source_ref`` scheme: ``expert:<slug>#<chunk_id>``, resolvable through
``experts.citation(slug, chunk_id)`` — which is also what gives a user the page
number and the file on disk.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

REF_PREFIX = "expert:"


def requested_slugs(req: RetrievalRequest) -> Tuple[str, ...]:
    """Every expert slug the runtime named, in order, deduplicated.

    A reference may be a bare ``expert:<slug>`` (search this corpus) or a full
    ``expert:<slug>#<chunk_id>`` (this exact chunk); both name the same corpus,
    so the slug is taken from either.
    """
    slugs: List[str] = []
    refs = list(req.refs_for(REF_PREFIX))
    for ref in tuple(req.request.task.required_evidence):
        value = str(ref or "").strip()
        if value.startswith(REF_PREFIX) and value not in refs:
            refs.append(value)
    for ref in refs:
        slug = ref[len(REF_PREFIX):].split("#", 1)[0].strip()
        if slug and slug not in slugs:
            slugs.append(slug)
    return tuple(slugs)


class ExpertSource(ThreadedSource):
    """``services/experts.py`` — a named specialist's corpus."""

    source_id = "experts"
    sections = ("retrieved_documents",)
    handles = (REF_PREFIX,)

    def available(self) -> bool:
        try:
            import services.experts as experts
        except Exception as exc:                               # noqa: BLE001
            logger.debug("services.experts unavailable: %s", exc)
            return False
        try:
            return bool(experts.experts_enabled())
        except Exception:                                      # noqa: BLE001
            return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("retrieved_documents"):
            return "retrieved_documents not requested"
        if not requested_slugs(req):
            return "no expert named in explicit_refs"
        if not str(req.query or "").strip():
            return "no query"
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        import services.experts as experts

        slugs = requested_slugs(req)
        if not slugs:
            return ()
        limit = req.top()
        owner = str(req.owner or "") or None
        # The per-source cap is shared across the named experts rather than
        # multiplied by them: two experts must not buy twice the budget.
        per_expert = max(1, limit // len(slugs))
        out: List[ContextCandidate] = []
        for slug in slugs:
            try:
                result = experts.search(slug, str(req.query or ""), per_expert,
                                        owner=owner, reranker=True)
            except Exception as exc:                           # noqa: BLE001
                # One broken index must not cost the other experts, and it must
                # certainly not cost the turn.
                logger.warning("expert %s search failed: %s", slug, exc)
                continue
            result = result if isinstance(result, Mapping) else {}
            tier = str(result.get("tier") or "lexical")
            degraded = bool(result.get("degraded")) or tier == "lexical"
            for hit in list(result.get("hits") or []):
                candidate = self._candidate(slug, hit, req, tier, degraded)
                if candidate is not None:
                    out.append(candidate)
        return tuple(out[:limit])

    def _candidate(self, slug: str, hit: Mapping[str, Any], req: RetrievalRequest,
                   tier: str, degraded: bool) -> Optional[ContextCandidate]:
        chunk_id = str(hit.get("chunk_id") or "")
        if not chunk_id:
            return None
        source = str(hit.get("source") or "")
        page = hit.get("page")
        where = (f"p. {page}" if isinstance(page, int)
                 else f"L{hit.get('start_line', 1)}-{hit.get('end_line', 1)}")
        lanes: Tuple[str, ...] = ("lexical",) if tier == "lexical" else ("lexical", "semantic")
        return make_candidate(
            source_type="expert",
            source_ref=f"{REF_PREFIX}{slug}#{chunk_id}",
            section="retrieved_documents",
            title=f"{slug}: {source} ({where})".strip(),
            body=str(hit.get("text") or ""),
            lanes=lanes,
            scores={"score": hit.get("score", 0.0)},
            trust_class="untrusted",
            authority="inference",
            source_revision=chunk_id,
            owner=req.owner,
            project_id=req.project_id,
            degraded=degraded,
            meta={"slug": slug, "source": source, "page": page, "tier": tier,
                  "start_line": hit.get("start_line"), "end_line": hit.get("end_line")},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        import services.experts as experts

        ref = str(source_ref or "")
        if not ref.startswith(REF_PREFIX) or "#" not in ref:
            return None
        slug, chunk_id = ref[len(REF_PREFIX):].split("#", 1)
        cite = experts.citation(slug, chunk_id)
        if not cite:
            return None
        page = cite.get("page")
        where = (f"p. {page}" if isinstance(page, int)
                 else f"L{cite.get('start_line', 1)}-{cite.get('end_line', 1)}")
        return make_candidate(
            source_type="expert", source_ref=ref, section="retrieved_documents",
            title=f"{slug}: {cite.get('source') or ''} ({where})".strip(),
            body=str(cite.get("excerpt") or ""),
            lanes=("exact",), trust_class="untrusted", authority="inference",
            source_revision=str(chunk_id), owner=req.owner, project_id=req.project_id,
            meta={"slug": slug, "source": str(cite.get("source") or ""), "page": page,
                  "file_path": str(cite.get("file_path") or ""),
                  "file_url": str(cite.get("file_url") or "")})


__all__ = ["ExpertSource", "requested_slugs", "REF_PREFIX"]
