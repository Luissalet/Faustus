# Scope comparator (Hangar E30)

`src/scope_compare.py` compares a client's new request with the agreed scope
(and, optionally, the first request) clause by clause.

## Statuses

| Status | When |
|---|---|
| included | the clause shares most words with an agreed clause and its formats, units and quantities are the same |
| changed | same clause, different format (PDF → DOCX), unit or language word (milímetros → pulgadas) or quantity (10 → 7 días) |
| added | no agreed clause shares enough words |
| removed | the clause asks to drop an agreed one ("ya no hace falta…", "quitar…"); in `mode=replace`, also agreed clauses the new text no longer mentions |
| ambiguous | vague wording ("más bonito", "mejorar", "etc."), two agreed clauses match equally, or only part of a clause overlaps; each becomes a question for the client |

Matching uses accent-folded words without stop words, short stems, formats,
unit words and quantities. It does not guess meaning.

## Effects and effort

With `graph` (a decision map / derivation graph from `decision_map` and
`derivation_graph`), each changed, added or removed clause is followed through
`affected_by` from the map items it names (only the changed attribute for a
change: "PDF" for PDF → DOCX). One derivation node to rerun is a **local**
change; most of the graph (four or more nodes, or 60 %) reruns the
**pipeline**; in between is **partial**. The links are returned as the reason.
Without a map, or without an item the clause names, the effect is
**unknown** and the options say what to link.

`effort_hours` (`{"node": [min, max]}`) is summed over the nodes to rerun;
nodes without a range are listed under `no_effort_for` and never filled in.

## Surfaces

| Surface | Name |
|---|---|
| Chat tool | `scope_compare` (`action` compare, list, show) |
| HTTP (admin) | `POST /api/scope-comparisons`, `GET /api/scope-comparisons`, `GET /api/scope-comparisons/{id}` |
| MCP | built-in server `delivery`, tool `scope_compare` |
| Studio | Delivery → **Scope changes**: agreed scope, new request, first request, decision map id, "adds to / restates" mode, hours per step; result table with status, agreed clause, effect (nodes to rerun, deliverables, hours) and options or the question to ask; questions for the client; what was negotiated; earlier comparisons |

Comparisons are kept in `DATA_DIR/scope_comparisons/<owner>/`.
