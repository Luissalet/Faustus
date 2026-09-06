"""
context_engine/adapters/provenance.py — the graph as a tiebreak, on a budget.

``src/provenance_graph.py`` is an audit view over declared edges: memories
pointing at the chat and file lines their evidence names, checkpoints pointing
at the files their diff actually changed, objectives pointing at the log
records that moved them.  It answers "why is this here", and that is exactly
the kind of thing a ``past_experiences`` or ``decisions`` section is for.

Two constraints shape every line below.

**The graph is rebuilt, not stored.**  ``build()`` walks objectives, the
objective log, memory, dispatch mirrors and expert citations on every call.
Doing that once per turn per source would be the single most expensive thing in
this package, so the result is cached by ``(owner, workspace)`` for a few
seconds — long enough that a turn's several rounds share one build, short
enough that a memory written this turn shows up in the next one.  The cache key
is the isolation boundary as well as the memo key: two owners can never share
an entry, because the owner is *in* the key rather than filtered out of the
value.

**Connectedness is not relevance.**  ``ranking_signal()`` returns degree
centrality normalised into ``[0, 0.10]`` and the module says why at length: a
graph measures how much has been *written about* a thing, not how well it
answers the question in front of you.  ``eidetic_engine_cli`` weights its own
far richer graph at 0.10 against 0.45 BM25 + 0.45 semantic, and
``memory_engine`` already retrieves on exactly those weights.  So the signal is
passed through unscaled, under the name ``graph``, and a ranker that adds it to
a lexical score cannot let it dominate — ``RANKING_CAP`` is respected by not
touching it.

Section and trust both follow the node kind, because the kinds are not
comparable: a checkpoint node was built from a diff observed on disk, an
objective from a human's typed delta, a memory from the learned store.  Giving
all three ``agent_claim`` would throw away the only thing the graph knows for
certain.

``source_ref`` scheme: ``prov:<node_id>``, and a node id is itself
``<kind>:<key>`` — so a full reference reads ``prov:checkpoint:job-17``.
Resolvable with ``explain(graph, node_id)``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

REF_PREFIX = "prov:"

#: Long enough for the several rounds of one turn to share a build, short
#: enough that a memory written a moment ago is visible on the next turn.
CACHE_TTL_S = 15.0

#: How many (owner, workspace) graphs to keep.  A long-lived server sees many.
CACHE_MAX_ENTRIES = 32

#: Node kinds this source speaks for, and where each belongs.  `file`, `expert`
#: and `corpus` nodes are deliberately absent: files.py and experts.py own
#: those, with the real contents rather than a label.
_SECTION_BY_KIND: Dict[str, str] = {
    "checkpoint": "past_experiences",
    "chat": "past_experiences",
    "memory": "past_experiences",
    "objective": "decisions",
}

#: (trust_class, authority) by node kind.  A checkpoint's edges come from a
#: diff the harness read off disk; an objective's from a typed delta a human
#: or an agent-under-a-human applied; a memory's from the learned store.
_TRUST_BY_KIND: Dict[str, Tuple[str, str]] = {
    "checkpoint": ("observed", "observed_state"),
    "objective": ("human_explicit", "binding_decision"),
    "memory": ("agent_validated", "validated_experience"),
    "chat": ("human_explicit", "human_memory"),
}

_LOCK = threading.RLock()
_CACHE: "Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]]" = {}


def clear_cache() -> None:
    """Drop every cached graph.  For tests, and for a process whose data
    directory has just changed underneath it."""
    with _LOCK:
        _CACHE.clear()


def _tokens(text: Any) -> set:
    cleaned = (str(text or "").lower()
               .replace("/", " ").replace("_", " ").replace("-", " "))
    return {t for t in cleaned.split() if len(t) > 2}


class ProvenanceSource(ThreadedSource):
    """``src/provenance_graph.py`` — declared edges, as evidence."""

    source_id = "provenance"
    sections = ("past_experiences", "decisions")
    handles = (REF_PREFIX,)

    def available(self) -> bool:
        try:
            import src.provenance_graph as graph
        except Exception as exc:                               # noqa: BLE001
            logger.debug("provenance_graph unavailable: %s", exc)
            return False
        try:
            return bool(graph.enabled())
        except Exception:                                      # noqa: BLE001
            return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.allows("graph") and not req.refs_for(REF_PREFIX):
            return "the graph lane is not open"
        if not (req.wants("past_experiences") or req.wants("decisions")):
            return "neither past_experiences nor decisions requested"
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        return ""

    # ── the cached build ───────────────────────────────────────────────────

    def _graph(self, req: RetrievalRequest) -> Dict[str, Any]:
        import src.provenance_graph as provenance

        owner = str(req.owner or "")
        workspace = str(req.workspace or "")
        key = (owner, workspace)
        now = time.monotonic()
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and now - cached[0] < CACHE_TTL_S:
                return cached[1]
        # Built outside the lock: this can take a second on a large store, and
        # holding the lock through it would serialise every other owner's turn.
        built = provenance.build(owner, workspace=workspace or None,
                                 limit_nodes=provenance.DEFAULT_LIMIT_NODES)
        if not isinstance(built, Mapping):
            built = {"nodes": [], "edges": [], "sources": {}, "truncated": False}
        with _LOCK:
            _CACHE[key] = (time.monotonic(), dict(built))
            if len(_CACHE) > CACHE_MAX_ENTRIES:
                stale = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
                for stale_key, _ in stale[:len(_CACHE) - CACHE_MAX_ENTRIES]:
                    _CACHE.pop(stale_key, None)
        return dict(built)

    # ── search ─────────────────────────────────────────────────────────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        import src.provenance_graph as provenance

        graph = self._graph(req)
        nodes = [n for n in (graph.get("nodes") or [])
                 if isinstance(n, Mapping) and n.get("kind") in _SECTION_BY_KIND]
        if not nodes:
            return ()

        try:
            signal = provenance.ranking_signal(graph)
        except Exception as exc:                               # noqa: BLE001
            logger.debug("provenance ranking unavailable: %s", exc)
            signal = {}

        wanted = _tokens(req.query)
        scored: List[Tuple[float, Mapping[str, Any]]] = []
        for node in nodes:
            section = _SECTION_BY_KIND[str(node.get("kind"))]
            if not req.wants(section):
                continue
            overlap = 0.0
            if wanted:
                text = _tokens(node.get("label")) | _tokens(node.get("detail"))
                hits = wanted & text
                if not hits:
                    # With a query, an unmatched node is noise: the graph is a
                    # tiebreak among things already relevant, not a way in.
                    continue
                overlap = len(hits) / len(wanted)
            centrality = float(signal.get(str(node.get("id")), 0.0))
            scored.append((overlap + centrality, node))

        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
        out: List[ContextCandidate] = []
        for score, node in scored[:req.top()]:
            candidate = self._candidate(node, req, provenance, graph,
                                        float(signal.get(str(node.get("id")), 0.0)),
                                        score, ("graph",))
            if candidate is not None:
                out.append(candidate)
        return tuple(out)

    def _candidate(self, node: Mapping[str, Any], req: RetrievalRequest,
                   provenance: Any, graph: Mapping[str, Any], centrality: float,
                   score: float, lanes: Tuple[str, ...]) -> Optional[ContextCandidate]:
        node_id = str(node.get("id") or "")
        kind = str(node.get("kind") or "")
        section = _SECTION_BY_KIND.get(kind)
        if not node_id or not section:
            return None
        trust, authority = _TRUST_BY_KIND.get(kind, ("agent_validated", "inference"))

        lines = [str(node.get("detail") or "").strip()]
        try:
            chain = provenance.explain(graph, node_id)
        except Exception as exc:                               # noqa: BLE001
            logger.debug("provenance explain(%s) failed: %s", node_id, exc)
            chain = {}
        summary = str((chain or {}).get("summary") or "").strip()
        if summary:
            lines.append(summary)
        for step in list((chain or {}).get("steps") or [])[:6]:
            why = str((step or {}).get("why") or "").strip()
            if why:
                lines.append(f"- {why}")
        body = "\n".join(line for line in lines if line)

        return make_candidate(
            source_type="experience" if section == "past_experiences" else "decision",
            source_ref=f"{REF_PREFIX}{node_id}",
            section=section,
            title=f"{kind}: {node.get('label') or node_id}",
            body=body,
            lanes=lanes,
            # `graph` is `ranking_signal`'s own number, already capped at 0.10
            # by that function; rescaling it here would silently undo the cap.
            scores={"graph": round(centrality, 6), "match": round(score, 6)},
            trust_class=trust,
            authority=authority,
            owner=req.owner,
            project_id=req.project_id,
            meta={"node_kind": kind, "node_id": node_id,
                  "graph_truncated": bool(graph.get("truncated")),
                  **{k: v for k, v in (node.get("meta") or {}).items()
                     if isinstance(v, (str, int, float, bool))}},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        import src.provenance_graph as provenance

        ref = str(source_ref or "")
        if not ref.startswith(REF_PREFIX):
            return None
        node_id = ref[len(REF_PREFIX):]
        graph = self._graph(req)
        for node in graph.get("nodes") or []:
            if isinstance(node, Mapping) and str(node.get("id")) == node_id:
                return self._candidate(node, req, provenance, graph, 0.0, 0.0,
                                       ("exact", "graph"))
        return None


__all__ = ["ProvenanceSource", "clear_cache", "CACHE_TTL_S", "CACHE_MAX_ENTRIES",
           "REF_PREFIX"]
