"""Mutual entity <-> source-fragment index for ``graph_ask``.

The Cerebro keeps two sides of one fact: the typed graph (entities and
relations) and the text it was read from (memory items, personal memories,
vault notes, each named by a ``source_ref`` such as ``mem:<id>``). The
``mentions`` table and the ``evidence`` of every relation already say which
fragment talks about which entity. This module reads both into one object
that can be walked in either direction:

* entity -> fragments: where is this thing written about?
* fragment -> entities: what is this text about?

The design idea (the graph and its source text must index each other, so a
graph hit can bring its text and a text hit can bring its graph) follows the
two-way entity/chunk linking of the KAG project (OpenSPG, Apache-2.0). The
code is Faustus' own and works on the existing tables; nothing is written.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.brain import entities
from src.brain.db import db, fold, loads

#: Folded words that carry no meaning for lexical scoring (ES + EN).
_STOP = frozenset(entities.FUNCTION_WORDS) | frozenset({"cual", "cuales", "cuantos", "cuantas", "que", "quien"})
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+#-]*")

#: A fragment longer than this is cut before it is scored or shown.
FRAGMENT_CHARS = 1200


def tokens(text: Any) -> List[str]:
    return [t for t in _TOKEN.findall(fold(text)) if t not in _STOP and len(t) > 1]


class MutualIndex:
    """A read-only snapshot of one owner's graph and its source fragments."""

    def __init__(self, owner: Any):
        self.owner = str(owner or "")
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.by_name: Dict[str, List[str]] = {}
        self.relations: List[Dict[str, Any]] = []
        self.out_edges: Dict[str, List[Dict[str, Any]]] = {}
        self.in_edges: Dict[str, List[Dict[str, Any]]] = {}
        self.entity_refs: Dict[str, List[str]] = {}
        self.ref_entities: Dict[str, List[str]] = {}
        self._text: Dict[str, str] = {}
        self._tokens: Dict[str, List[str]] = {}
        self._idf: Optional[Dict[str, float]] = None
        self._avg_len = 0.0

    # -- build ---------------------------------------------------------------------------

    @classmethod
    def build(cls, owner: Any) -> "MutualIndex":
        index = cls(owner)
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, type, aliases, summary FROM entities "
                "WHERE owner = ? AND hidden = 0 AND merged_into = ''", (index.owner,)).fetchall()
            mention_rows = conn.execute(
                "SELECT entity_id, source_ref FROM mentions WHERE owner = ? ORDER BY created_at",
                (index.owner,)).fetchall()
        for row in rows:
            aliases = [str(a) for a in loads(row["aliases"], []) if str(a).strip()]
            index.entities[row["id"]] = {"id": row["id"], "name": row["name"], "type": row["type"],
                                         "aliases": aliases, "summary": row["summary"] or ""}
            for label in [row["name"], *aliases]:
                key = fold(label)
                if key and row["id"] not in index.by_name.setdefault(key, []):
                    index.by_name[key].append(row["id"])
        for row in mention_rows:
            index._link(row["entity_id"], row["source_ref"])
        for relation in entities.list_relations(index.owner, include_closed=False):
            src, dst = relation["src"], relation.get("dst") or ""
            if src not in index.entities or (dst and dst not in index.entities):
                continue
            if relation.get("status") not in (None, "active"):
                continue
            index.relations.append(relation)
            index.out_edges.setdefault(src, []).append(relation)
            if dst:
                index.in_edges.setdefault(dst, []).append(relation)
            for ref in relation.get("evidence") or []:
                index._link(src, ref)
                if dst:
                    index._link(dst, ref)
        return index

    def _link(self, entity_id: str, ref: Any) -> None:
        ref = str(ref or "")
        if not ref or entity_id not in self.entities:
            return
        refs = self.entity_refs.setdefault(entity_id, [])
        if ref not in refs:
            refs.append(ref)
        ents = self.ref_entities.setdefault(ref, [])
        if entity_id not in ents:
            ents.append(entity_id)

    # -- entities ------------------------------------------------------------------------

    def name_of(self, entity_id: str) -> str:
        return (self.entities.get(entity_id) or {}).get("name", "")

    def schema(self, *, names: int = 40) -> Dict[str, Any]:
        """What a planner needs to know about this graph: the entity types and
        relation labels that exist and the best-connected names."""
        degree: Dict[str, int] = {}
        for eid in self.entities:
            degree[eid] = len(self.out_edges.get(eid, [])) + len(self.in_edges.get(eid, []))
        ranked = sorted(self.entities, key=lambda e: (-degree[e], self.name_of(e)))
        rels: Dict[str, int] = {}
        for relation in self.relations:
            rels[relation["rel"]] = rels.get(relation["rel"], 0) + 1
        types: Dict[str, int] = {}
        for entity in self.entities.values():
            types[entity["type"]] = types.get(entity["type"], 0) + 1
        return {"types": types, "relations": rels, "entities": len(self.entities),
                "fragments": len(self.ref_entities),
                "names": [{"name": self.name_of(e), "type": self.entities[e]["type"]} for e in ranked[:names]]}

    def find(self, name: Any, *, type: str = "", limit: int = 5) -> List[str]:  # noqa: A002
        """Entity ids a written name can mean, best first: the exact name or
        alias, then every entity whose label contains it (or is contained by
        it) at a word boundary, then token overlap."""
        key = fold(name)
        if not key:
            return []
        found: List[Tuple[float, str]] = []
        seen = set()

        def add(score: float, eid: str) -> None:
            if eid in seen:
                return
            if type and self.entities[eid]["type"] != type:
                score -= 0.25
            seen.add(eid)
            found.append((score, eid))

        for eid in self.by_name.get(key, []):
            add(3.0, eid)
        words = set(tokens(key))
        for label, ids in self.by_name.items():
            if label == key:
                continue
            if re.search(rf"\b{re.escape(key)}\b", label) or re.search(rf"\b{re.escape(label)}\b", key):
                for eid in ids:
                    add(2.0 - min(0.9, abs(len(label) - len(key)) / 40), eid)
            elif words:
                label_words = set(tokens(label))
                # every word of one side must be on the other ("Ana Gomez" / "Gomez Ana"); a single shared
                # word ("project") never makes two different names the same thing
                if label_words and (words <= label_words or label_words <= words):
                    for eid in ids:
                        add(1.0 + len(words & label_words) / 10, eid)
        found.sort(key=lambda pair: (-pair[0], self.name_of(pair[1])))
        return [eid for _s, eid in found[:max(1, limit)]]

    def entities_in(self, text: Any, *, limit: int = 8) -> List[str]:
        """Entities named in a free text (word-boundary, folded, longest label first)."""
        body = fold(text)
        if not body:
            return []
        labels = sorted(self.by_name, key=len, reverse=True)
        out: List[str] = []
        for label in labels:
            if len(label) < 2 or label in entities.FUNCTION_WORDS:
                continue
            if re.search(rf"\b{re.escape(label)}\b", body):
                for eid in self.by_name[label]:
                    if eid not in out:
                        out.append(eid)
            if len(out) >= limit:
                break
        return out[:limit]

    # -- fragments -----------------------------------------------------------------------

    def fragment_text(self, ref: str) -> str:
        if ref not in self._text:
            text = ""
            try:
                fact = entities._fact_from_source(self.owner, ref)  # noqa: SLF001 - the one place that resolves a ref
                text = str((fact or {}).get("text") or "").strip()
            except Exception:  # noqa: BLE001 - a source that cannot be read is just absent
                text = ""
            self._text[ref] = text[:FRAGMENT_CHARS]
        return self._text[ref]

    def fragments(self) -> List[str]:
        """Every reference that has readable text."""
        return [ref for ref in self.ref_entities if self.fragment_text(ref)]

    def fragment_entities(self, ref: str) -> List[str]:
        return list(self.ref_entities.get(ref, []))

    def entity_fragments(self, entity_id: str) -> List[str]:
        return list(self.entity_refs.get(entity_id, []))

    # -- lexical scoring (BM25 over the linked fragments) -----------------------------------

    def _stats(self) -> None:
        if self._idf is not None:
            return
        docs = self.fragments()
        df: Dict[str, int] = {}
        total = 0
        for ref in docs:
            toks = tokens(self.fragment_text(ref))
            self._tokens[ref] = toks
            total += len(toks)
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1
        n = max(len(docs), 1)
        self._avg_len = total / n if docs else 1.0
        self._idf = {tok: math.log(1 + (n - c + 0.5) / (c + 0.5)) for tok, c in df.items()}

    def text_scores(self, query: Any, *, refs: Optional[Iterable[str]] = None, k1: float = 1.4,
                    b: float = 0.75) -> Dict[str, float]:
        """BM25 score of every (or the given) fragment against `query`;
        fragments without a query token are left out."""
        self._stats()
        q = tokens(query)
        if not q:
            return {}
        pool = list(refs) if refs is not None else list(self._tokens)
        scores: Dict[str, float] = {}
        for ref in pool:
            toks = self._tokens.get(ref)
            if toks is None:
                toks = tokens(self.fragment_text(ref))
                self._tokens[ref] = toks
            if not toks:
                continue
            tf: Dict[str, int] = {}
            for tok in toks:
                tf[tok] = tf.get(tok, 0) + 1
            score = 0.0
            for tok in set(q):
                f = tf.get(tok)
                if not f:
                    continue
                idf = (self._idf or {}).get(tok, 0.5)
                score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(toks) / (self._avg_len or 1)))
            if score > 0:
                scores[ref] = score
        return scores


def weighted_merge(first: Dict[str, float], second: Dict[str, float], alpha: float = 0.5) -> Dict[str, float]:
    """Fuse two score tables: each is min-max normalised to [0, 1] on its own,
    then added as ``alpha * first + (1 - alpha) * second``. An id present in
    only one table keeps that table's share. (The same scheme the KAG merger
    uses for graph and text hits.)"""
    def norm(table: Dict[str, float]) -> Dict[str, float]:
        if not table:
            return {}
        low, high = min(table.values()), max(table.values())
        if math.isclose(low, high, rel_tol=1e-9, abs_tol=1e-12):
            return {k: 1.0 for k in table}
        return {k: (v - low) / (high - low) for k, v in table.items()}

    a, b = norm(first), norm(second)
    alpha = min(1.0, max(0.0, float(alpha)))
    merged: Dict[str, float] = {}
    for key, value in a.items():
        merged[key] = value * alpha
    for key, value in b.items():
        merged[key] = merged.get(key, 0.0) + value * (1 - alpha)
    return merged
