"""
context_engine/adapters/memory.py — two memories, one section, opposite rules.

Faustus has two memory stores and they are not versions of each other.
``src/memory.py`` is a flat list of facts a person (or an extractor) wrote into
``memory.json``; it never changes its mind and it is *about the user*.
``src/memory_engine.py`` is the learned store: rules with a maturity, evidence
spans, helpful/harmful feedback, and inverted anti-patterns; it is *about the
work*.  Both fill ``retrieved_memory``, so they are two sources here rather
than one adapter with a flag — a single class would have had to carry a
conditional in every method, and the conditional that matters (incognito) would
have been the easiest one to get wrong.

Three decisions worth the reader's time:

**Neither store is consulted when ``allow_personal_memory`` is false.**
``ContextPolicy`` says that flag is what incognito means, and the reason it is
a pre-retrieval gate rather than a post-retrieval filter is mechanical, not
philosophical: ``memory_engine.search()`` calls ``touch()`` on its hits and
``MemoryManager.increment_uses`` does the same thing for the classic store.
Searching and then discarding the results still writes ``last_used`` to disk,
still shifts the curator's decay maths, and still leaves a timing signal.  The
learned store is gated too, and not only the "personal" one: an owner-scoped
rule distilled from that owner's sessions is personal by any reading that
matters.

**Retrieval never marks a row as used.**  ``search(touch_hits=False)`` — a
candidate that the compiler drops for budget was not used, and telling the
curator otherwise is training it on a lie.  Use is recorded once, from
``ContextReceipt``, against the items that actually reached the model.

**The no-query path does not go through ``pack_detail``.**  A retrieval with no
query still owes the caller the standing procedural rules and the anti-patterns
— neither is query-dependent, and BM25 has nothing to match on.  ``pack_detail``
builds exactly that block, but it also ``touch()``es every id it returns, which
is the thing the paragraph above says not to do.  So the selection is rebuilt
here from ``scoped_items()``, which is a read.

``source_ref`` schemes:

    mem:<item_id>       a row in memory_engine.db; reopen with get_item()
    pmem:<entry_id>     an entry in memory.json; reopen with MemoryManager.load()

The second prefix is not in the original plan, which named only ``mem:``.  Two
stores with one prefix would make ``mem:abc`` ambiguous, and an ambiguous
reference is not provenance.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: How a learned item's own trust class picks its authority.  §14.2 decides
#: contradictions by authority, so this mapping is what makes "the user told me"
#: beat "a run inferred it" without either one being scored differently.
_AUTHORITY_BY_TRUST: Dict[str, str] = {
    "observed": "observed_state",
    "human_explicit": "human_memory",
    "proved": "proved_result",
    "agent_validated": "validated_experience",
    "agent_assertion": "agent_claim",
    "legacy_import": "agent_claim",
    "untrusted": "inference",
}

#: Levels that behave like standing rules rather than recollections.  These are
#: the ones the no-query path returns.
_STANDING_LEVELS: Tuple[str, ...] = ("procedural",)


def _row_lanes(row: Mapping[str, Any]) -> Tuple[str, ...]:
    """Which lanes actually contributed to this hit.

    Read off the row's own per-lane scores rather than assumed from
    configuration: when the vector store is down ``memory_engine`` renormalises
    the lexical weight to 0.90 and reports ``degraded``, and a candidate that
    claimed a semantic lane it never had would make the packet's manifest wrong
    in precisely the way the manifest exists to prevent.
    """
    lanes: List[str] = []
    if float(row.get("lexical") or 0.0) > 0:
        lanes.append("lexical")
    if float(row.get("semantic") or 0.0) > 0 and not row.get("degraded"):
        lanes.append("semantic")
    if float(row.get("graph") or 0.0) > 0:
        lanes.append("graph")
    return tuple(lanes) or ("lexical",)


class MemoryEngineSource(ThreadedSource):
    """``src/memory_engine.py`` — learned rules, memories and anti-patterns."""

    source_id = "memory_engine"
    sections = ("retrieved_memory",)
    handles = ("mem:",)

    def available(self) -> bool:
        try:
            import src.memory_engine  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("memory_engine unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_personal_memory:
            return "policy.allow_personal_memory is false"
        if not req.wants("retrieved_memory"):
            return "retrieved_memory not requested"
        return ""

    # ── scope ──────────────────────────────────────────────────────────────

    @staticmethod
    def _scope(req: RetrievalRequest) -> Tuple[str, str]:
        """``(owner, project)`` as ``memory_engine`` understands them.

        Its ``project`` column holds a WORKSPACE PATH, not a project id — see
        ``src/tool_execution.py``, which writes it — so the workspace wins and
        ``project_id`` is only a fallback for a request that has no workspace.

        Both are passed as strings and never as ``None``: ``scoped_items()``
        reads ``None`` as "do not filter", which on the owner column is the
        cross-tenant leak this whole layer exists to make impossible.  An empty
        owner therefore selects the unscoped rows only, which is correct.
        """
        return str(req.owner or ""), str(req.workspace or req.project_id or "")

    # ── search ─────────────────────────────────────────────────────────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        import src.memory_engine as engine

        owner, project = self._scope(req)
        limit = req.top()
        query = str(req.query or "").strip()

        if query and req.allows("lexical"):
            rows = engine.search(query, owner=owner, project=project, k=limit,
                                 statuses=("active", "anti_pattern"),
                                 touch_hits=False)
        elif req.allows("mandatory"):
            rows = self._standing(engine, owner, project, limit)
        else:
            return ()

        out: List[ContextCandidate] = []
        for row in rows:
            candidate = self._candidate(row, req, owner, project)
            if candidate is not None:
                out.append(candidate)
        return tuple(out[:limit])

    @staticmethod
    def _standing(engine: Any, owner: str, project: str,
                  limit: int) -> List[Dict[str, Any]]:
        """Procedural rules and anti-patterns, best first, with no query.

        Anti-patterns are deliberately not score-filtered: an inverted rule has
        a deeply negative score BY CONSTRUCTION — that is why it was inverted —
        and the warning is the entire reason it was kept.
        """
        items = engine.scoped_items(owner, project, ("active", "anti_pattern"))
        rows = [engine.public_item(item) for item in items]
        rules = [r for r in rows
                 if r.get("status") == "active"
                 and r.get("level") in _STANDING_LEVELS
                 and float(r.get("effective_score") or 0.0) > 0]
        antis = [r for r in rows if r.get("status") == "anti_pattern"]
        rules.sort(key=lambda r: (-float(r.get("effective_score") or 0.0), r.get("id") or ""))
        antis.sort(key=lambda r: (-float(r.get("effective_score") or 0.0), r.get("id") or ""))
        return (rules + antis)[:limit]

    def _candidate(self, row: Mapping[str, Any], req: RetrievalRequest,
                   owner: str, project: str) -> Optional[ContextCandidate]:
        item_id = str(row.get("id") or "")
        if not item_id:
            return None
        anti = row.get("status") == "anti_pattern"
        trust = str(row.get("trust_class") or "agent_assertion")
        authority = ("validated_experience" if anti
                     else _AUTHORITY_BY_TRUST.get(trust, "agent_claim"))
        level = str(row.get("level") or "semantic")
        maturity = str(row.get("maturity") or "candidate")
        title = (f"anti-pattern [{item_id[:8]}]" if anti
                 else f"{level} memory [{item_id[:8]}] ({maturity})")
        scores = {
            "relevance": row.get("relevance", 0.0),
            "lexical": row.get("lexical", 0.0),
            "semantic": row.get("semantic", 0.0),
            "graph": row.get("graph", 0.0),
            "effective_score": row.get("effective_score", 0.0),
            "harmful_ratio": row.get("harmful_ratio", 0.0),
        }
        return make_candidate(
            source_type="memory",
            source_ref=f"mem:{item_id}",
            section="retrieved_memory",
            title=title,
            body=str(row.get("text") or ""),
            lanes=_row_lanes(row) if row.get("relevance") is not None else ("mandatory",),
            scores=scores,
            trust_class=trust,
            authority=authority,
            source_revision=str(row.get("updated_at") or ""),
            observed_at=row.get("updated_at") or row.get("created_at") or "",
            owner=str(row.get("owner") or owner),
            project_id=req.project_id,
            degraded=bool(row.get("degraded")),
            meta={
                "anti_pattern": bool(anti),
                "level": level,
                "maturity": maturity,
                "status": str(row.get("status") or ""),
                "workspace_scope": project,
                "helpful_count": int(row.get("helpful_count") or 0),
                "harmful_count": int(row.get("harmful_count") or 0),
            },
        )

    # ── fetch ──────────────────────────────────────────────────────────────

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        import src.memory_engine as engine

        ref = str(source_ref or "")
        if not ref.startswith("mem:"):
            return None
        item = engine.get_item(ref[4:])
        if not item:
            return None
        owner, project = self._scope(req)
        # The scope check the search path gets from `scoped_items`, restated
        # for the by-id path: an id can come from anywhere, including a model.
        stored_owner = str(item.get("owner") or "")
        if stored_owner and owner and stored_owner != owner:
            logger.debug("refusing mem:%s - owned by another actor", item.get("id"))
            return None
        return self._candidate(engine.public_item(item), req, owner, project)


class PersonalMemorySource(ThreadedSource):
    """``src/memory.py`` — the flat ``memory.json`` facts about the user.

    Kept separate from the learned store because the two disagree about what a
    memory is: these entries never decay, never invert, and are what the user
    would point at if asked "what does it know about me".  They are also the
    entries an incognito turn most obviously must not see, which is why the
    gate is the first line of every method that would otherwise open the file.
    """

    source_id = "personal_memory"
    sections = ("retrieved_memory",)
    handles = ("pmem:",)

    #: Jaccard floor from ``MemoryManager.get_relevant_memories``; below this
    #: the store itself considers an entry unrelated to the query.
    RELEVANCE_THRESHOLD = 0.05

    def __init__(self, manager: Any = None, data_dir: str = "") -> None:
        # Nothing is opened here.  `MemoryManager.__init__` calls
        # `ensure_file_exists()`, which WRITES an empty memory.json, and a
        # constructor that creates a file in the user's data directory just
        # because a source list was built is a surprise nobody asked for.
        self._manager = manager
        self._data_dir = str(data_dir or "")

    def available(self) -> bool:
        if self._manager is not None:
            return True
        try:
            import src.memory  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("src.memory unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_personal_memory:
            return "policy.allow_personal_memory is false"
        if not req.wants("retrieved_memory"):
            return "retrieved_memory not requested"
        return ""

    def _store(self) -> Any:
        if self._manager is None:
            from src.constants import DATA_DIR
            from src.memory import MemoryManager
            self._manager = MemoryManager(self._data_dir or DATA_DIR)
        return self._manager

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        owner = str(req.owner or "")
        store = self._store()
        # `load(owner)` filters on the owner column; `load(None)` returns
        # everything, so the argument is always passed and never defaulted.
        entries = list(store.load(owner) or [])
        if not entries:
            return ()
        limit = req.top()
        query = str(req.query or "").strip()
        if query and req.allows("lexical"):
            chosen = store.get_relevant_memories(
                query, entries, threshold=self.RELEVANCE_THRESHOLD, max_items=limit)
            lanes: Tuple[str, ...] = ("lexical",)
        elif req.allows("mandatory"):
            chosen = entries[:limit]
            lanes = ("mandatory",)
        else:
            return ()

        out: List[ContextCandidate] = []
        for entry in chosen or []:
            candidate = self._candidate(entry, req, owner, lanes)
            if candidate is not None:
                out.append(candidate)
        return tuple(out[:limit])

    def _candidate(self, entry: Mapping[str, Any], req: RetrievalRequest,
                   owner: str, lanes: Tuple[str, ...]) -> Optional[ContextCandidate]:
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            return None
        source = str(entry.get("source") or "").lower()
        # An entry the user typed is a human statement; one the extractor wrote
        # is a model's reading of a conversation, and the two must not carry the
        # same weight into a contradiction.
        human = source in ("user", "manual", "inline", "")
        return make_candidate(
            source_type="memory",
            source_ref=f"pmem:{entry_id}",
            section="retrieved_memory",
            title=f"{entry.get('category') or 'fact'} [{entry_id[:8]}]",
            body=str(entry.get("text") or ""),
            lanes=lanes,
            scores={"uses": float(entry.get("uses") or 0)},
            trust_class="human_explicit" if human else "agent_assertion",
            authority="human_memory" if human else "agent_claim",
            source_revision=str(entry.get("timestamp") or ""),
            observed_at=entry.get("timestamp") or "",
            owner=str(entry.get("owner") or owner),
            project_id=req.project_id,
            meta={"category": str(entry.get("category") or ""),
                  "entry_source": source, "store": "memory.json"},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        ref = str(source_ref or "")
        if not ref.startswith("pmem:"):
            return None
        wanted = ref[5:]
        owner = str(req.owner or "")
        for entry in self._store().load(owner) or []:
            if str(entry.get("id") or "") == wanted:
                return self._candidate(entry, req, owner, ("exact",))
        return None


__all__ = ["MemoryEngineSource", "PersonalMemorySource"]
