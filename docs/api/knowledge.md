# Knowledge neighborhood: why a file or requirement matters (CMP-04)

`src/knowledge_neighborhood.py` — one read across four stores this repo
already has (`src/requirements/`, `src/project_board.py`,
`src/context_engine/code_index.py`, `src/requirements/evidence.py`) that
answers, for one file or requirement: what it must satisfy, which decisions
condition it, what tests cover it, and what fell out of date — typed
`requirement -> decision(issue) -> symbol -> test -> run`, never a second
index. No LLM call anywhere in this module.

## Endpoint

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/projects/{project_id}/knowledge/neighborhood` | `path?`, `req_key?`, `issue_key?`, `depth?` (1-3, default 1) | `{"neighborhood": {...}}` |

Owner-scoped via `require_user` + the same `_project_or_404` helper
`routes/requirements_routes.py` and `routes/board_routes.py` already use — a
project id that is not this owner's answers exactly like one that does not
exist. At least one of `path`/`req_key`/`issue_key` should be given; with
none the result is an empty, well-formed neighborhood, never "every
requirement in the project" standing in for scope.

### Response shape

```json
{
  "neighborhood": {
    "project_id": "…", "path": "src/auth.py", "req_key": null, "issue_key": null,
    "depth": 1,
    "nodes": [
      {"id": "requirement:REQ-3", "type": "requirement", "label": "REQ-3: Users can log in",
       "ref": "REQ-3", "why": "…", "stale": false,
       "status": "accepted", "verified": true, "implemented": true, "tested": true},
      {"id": "decision:FAU-9", "type": "decision", "label": "FAU-9: Add login",
       "ref": "FAU-9", "why": "declarado por REQ-3", "stale": false, "status": "open"},
      {"id": "symbol:src/auth.py@login", "type": "symbol", "ref": "src/auth.py@login",
       "why": "enlazado por REQ-3 (implements)", "stale": false},
      {"id": "test:tests/test_auth.py@test_login", "type": "test", "stale": false},
      {"id": "run:run_123", "type": "run", "why": "evidencia registrada por REQ-3", "stale": false}
    ],
    "edges": [
      {"src": "requirement:REQ-3", "dst": "decision:FAU-9", "relation": "declared", "kind": "issue", "stale": false, "why": ""},
      {"src": "requirement:REQ-3", "dst": "symbol:src/auth.py@login", "relation": "declared", "kind": "implements", "stale": false, "why": ""},
      {"src": "requirement:REQ-3", "dst": "test:tests/test_auth.py@test_login", "relation": "declared", "kind": "tests", "stale": false, "why": ""},
      {"src": "requirement:REQ-3", "dst": "run:run_123", "relation": "verified", "kind": "evidences", "stale": false, "why": ""}
    ],
    "unknown": [],
    "stale_refs": []
  }
}
```

`unknown` carries any requested `req_key`/`issue_key` that does not exist —
never answered with a fabricated neighborhood for an id that was not found
(same rule `requirements/context.py::for_task`'s own `unknown` list
follows). `stale_refs` is the deduplicated headline list of every stale
node's `ref`, so a caller does not have to re-walk `nodes` to answer "is
anything here out of date".

## `relation`: never invented, always one of three

- **`declared`** — a human or agent recorded this link on purpose: a
  requirement's `implements`/`tests`/`issue` link
  (`src/requirements/store.py`'s `LINK_KINDS`).
- **`located`** — found mechanically: a code-index structural neighbor
  (`code_index.neighbors`) or an issue found by a text search over the
  board (`project_board.list_issues(q=…)`) that mentions the path/key but
  carries no explicit link.
- **`verified`** — an `evidences` link resolves against the requirement's
  CURRENT revision. This is the ONLY relation this module ever calls
  `verified` — the same rule `src/requirements/evidence.py`'s own docstring
  states (only `kind == "evidences"` sets it, never an `implements` link or
  an `@implements` comment). A requirement node's own `verified` field
  mirrors `evidence.matrix()['verified']` exactly; nothing here recomputes
  that judgement differently.

`stale: bool` sits on both nodes and edges, and is never left implicit. It
is computed by delegating to `evidence.resolve_link_state()` for every
`requirement -> {decision,symbol,test,run}` edge — never recomputed here —
so the four staleness reasons `evidence.py` already names
(`content_changed`, `symbol_not_found`, `target_missing`,
`requirement_changed_since_verification`) are translated once, into a
short Spanish `why`, and repeated nowhere else:

| `evidence.py` reason | `why` shown |
| --- | --- |
| `content_changed` | el fichero cambió desde que se enlazó |
| `symbol_not_found` | el símbolo ya no está en el fichero |
| `target_missing` | el objetivo del enlace ya no existe |
| `path_outside_workspace` | el objetivo queda fuera del workspace |
| `requirement_changed_since_verification` | evidencia caducada: el requisito cambió desde que se verificó |

A target that no longer exists at all resolves to `unknown` in
`evidence.py`'s own terms (`evidence.matrix.stale` counts only `"stale"`
`live_state` rows) and is **not** marked `stale` here either — "borrar test
→ degrada" and "cambiar requisito tras verificar → stale" stay two
different outcomes, exactly as `evidence.py` distinguishes them.

## The decisive scenario

Requirement changes AFTER its evidence was verified, while the file it
implements and the test that covers it are both untouched ("test antiguo en
verde, código sin actualizar"): the requirement's `evidences` link goes
`stale` (`evidence.resolve_link_state` already computes this — see
`requirement_changed_since_verification` above), so:

- the `requirement -> run` edge (`kind="evidences"`) carries `stale: true`
  and a `why` containing "evidencia caducada";
- the requirement node's `verified` is `false` — `evidence.matrix()`'s own
  `_any('evidences')` only counts a `live_state == "linked"` row, and a
  stale one is not that, so this module never has to special-case it;
- `implemented`/`tested` on the requirement node stay `true`: the code and
  the test are fine, only the evidence is out of date, and the
  neighborhood says exactly that, not less and not more.

Covered by `tests/test_cmp04_knowledge.py::test_decisive_stale_evidence_is_never_reported_verified`.

## Sources, one read each

- **Requirements**: `req_store.all_requirement_keys` + `req_store.list_links`
  (matching a `path` against a declared `implements`/`tests` link's target
  prefix — same match `requirements/context.py::_linked_keys_for_files`
  uses, kept as its own pass here because that helper is private and this
  one also needs the matching links, not just the keys), and
  `evidence.matrix()` for every resolved requirement (never recomputes
  linked/implemented/tested/verified/stale — reads them straight off).
- **Decisions**: `project_board.get(issue_id)` for a requirement's declared
  `issue` link, and `project_board.list_issues(project_id, q=…)` for a
  text-mention search when a `path` is given — the latter always
  `relation="located"`, `kind="mentions"`, never conflated with a real
  link.
- **Symbols/tests**: `code_index.symbols_in(path, …)` for the file's own
  definitions, then `code_index.neighbors(symbol_id, hops=depth)` for
  their structural neighbors (`imports`/`calls`/`defines`/`tests`/
  `registers` — `code_index.EDGE_KINDS`), all `relation="located"`. A node
  whose path/qualname looks like a test (`test_`, `/tests/`, `_test.py`)
  is typed `test` rather than `symbol`, the same heuristic
  `knowledge_neighborhood._looks_like_test` names once and reuses for both
  the requirement-link path and the code-index path.
- **Runs**: an `evidences` link's `target` becomes a `run` node — this
  module does not resolve it against `agent_runs.py`/`changeset_store.py`
  further; the id is shown as-is, exactly as `evidence.py` stores it.

Bounded: `depth` clamps to `MAX_DEPTH=3`; `MAX_NODES=400`/`MAX_EDGES=800`
cap one response the same way `context_engine/wiring.py`'s own
`MAX_REPORT_ROWS` caps a shadow report — a neighborhood with thousands of
callers has already made its point in the first few hundred.

## Studio

- `studio/src/adapters/knowledge.ts` — `getNeighborhood(projectId, {path,
  reqKey, issueKey, depth}, signal)`, typed `NeighborNode`/`NeighborEdge`/
  `Neighborhood`.
- `studio/src/screens/Project.tsx` — "File neighborhood" (`FileNeighborhood`
  component) in the project's Context tab: a path in, four grouped lists out
  (what it must satisfy / decisions that condition it / tests that cover it
  / out of date).

## Context receipts (`event: context_receipts`, `src/agent_loop.py`)

A related but separate CMP-04 piece, NOT part of this endpoint: a per-turn
summary of what the turn's delivered context packets actually put in front
of the model, built from `context_engine.wiring.deliver_round`'s own report
(`src/context_engine/wiring.py` — read-only from this lot; nothing there was
changed). Rows are `{source, kind, ref, why}`, deduplicated by `(kind, ref)`
across the turn's rounds.

- Persisted onto the turn's metadata as `metrics["context_receipts"]` — the
  same path `metrics["harness"]` already rides into
  `routes/chat_helpers.py::save_assistant_response`'s `md` (that file is
  untouched; `agent_loop.py`'s `metrics` dict already flows straight into
  `last_metrics` there, so adding a key to `metrics` was the whole change).
- Streamed once per turn as `data: {"type": "context_receipts", "data": [...]}`,
  capped at 40 rows.
- Decoded in `studio/src/adapters/chat.ts` (`ContextReceipt`, the
  `context_receipts` `ChatEvent` variant, `case 'context_receipts':` in
  `decode()`).
- Rendered in `studio/src/screens/studio/Transcript.tsx` as a collapsed
  "Contexto usado (n) — por qué" card (`ContextReceiptCard`) on a settled
  assistant turn, with a link to the source when `ref` is a URL.

See "Puntos a cablear por el orquestador" in
`docs/adaptations/decisions/CMP-04.md` for the one piece this lot could not
finish inside its own file list: `ContextReceiptCard` reads
`turn.contextReceipts` defensively (an `unknown` cast), because the
live-stream reducer that would populate that field on `Turn` lives in
`studio/src/screens/studio/model.ts`, outside W2-D's file list.
