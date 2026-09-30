# Run records: effects, ledger, turn cost, steering receipts, orphans

Four durable records answer "what did this run do, what did it cost, and what did
it leave open". None of the routes below runs anything again; they read, or (where
stated) record a person's decision.

| Record | Store | Module |
|---|---|---|
| Effect outbox | `effect_outbox.sqlite3` (WAL, `synchronous=FULL`) | `src/effect_outbox.py`, `src/effect_tools.py` |
| Execution ledger | `exec_ledger.sqlite3` (append-only, `synchronous=NORMAL`) | `src/exec_ledger.py` |
| Steering receipts | rows of the ledger (`steer_*`) | `src/steering_journal.py` |
| Turn cost | derived from the ledger | `src/turn_cost.py` |

Settings: `agent_effect_outbox` and `agent_exec_ledger` (both on). Turning one off
restores the plain path and records nothing; the route still answers.

## Routes

All routes need a user and are scoped to the caller (a session you do not own is a 404).

| Route | What it returns |
|---|---|
| `GET /api/effects` | Effects that leave the machine (mail, messages, webhooks, calendar/HTTP writes, connector writes) with `state`, `effect_certainty`, `attempt_id`. Filters: `state`, `session_id`, `kind`, `unresolved=true` (outcome unknown or partial). |
| `GET /api/effects/{id}` | One effect and its transition history. |
| `POST /api/effects/{id}/reconcile` | Ask the destination by identifier. Never sends again. Absence proves nothing unless the finder says it is authoritative. |
| `POST /api/effects/{id}/resolve` | `{"landed": bool, "note": str}`: a person records what they found. |
| `GET /api/runs/{session_id}/ledger` | The session's runs; with `run_id`, that run replayed call by call (`events=true` adds the raw rows). |
| `GET /api/runs/{session_id}/turn-cost` | Where one turn's time and tokens went, by cause. `run_id` picks the turn, else `turn=0` is the latest with work, `1` the one before. |
| `GET /api/agent/orphans` | Workers whose parent run is gone (`finished`, `replaced`, `gone`), with why. |
| `POST /api/agent/orphans/{key}/kill` | Stop one listed orphan. Refuses anything that is not currently an orphan. |
| `POST /api/chat/steer/{session_id}` | Unchanged, plus `receipt` in the answer. |
| `GET /api/chat/steer/{session_id}/receipts` | Every message sent to a live run and its state. |
| `POST /api/chat/steer/{session_id}/claim` | Hands the client the "send after" messages whose run has ended (none while a run is active or a card is pending). |
| `POST /api/chat/steer/receipt/{id}/ack` | The client sent the claimed message as a turn. |

Agents reach the same four views through the built-in tool `run_report`
(`view` = `effects`, `ledger`, `cost`, `orphans`) and the workers MCP server's
`run_report`.

## Effect states

`prepared` -> `admitted` -> `dispatching` -> `succeeded` | `failed_before_effect` |
`partial` | `outcome_unknown` -> `reconciled`; `cancelled` is its own end.
`effect_certainty` is separate: `none`, `confirmed`, `partial`, `unknown`.
The intent is committed and read back before anything is sent; if it cannot be, the
effect is not sent. `dispatching` is written before the transport is touched, so a
crash after that point is `outcome_unknown` on the next boot, never "not executed".
Every tool result carries `attempt_id` and `effect_certainty`.

## Steering receipt states

`queued` (accepted) -> `drained` (the run took it) -> `applied` (the loop appended it,
or the client confirmed it sent the turn). `dropped` carries a reason and keeps the
text; `redelivered` means a dropped message was put in front of a later turn.
Nothing reads `queued` or `drained` as delivered. After a restart every message the
old process left unresolved is dropped with a reason.

## Turn cost

Phases, in causal order: `main_rounds`, `tools`, `compaction`, `recovery`, `advisor`,
`workers`, `retries`, `other_model`. A total is the sum of the calls that reported the
value; `*_unknown_calls` counts the ones that did not; a value nobody reported is
`null`, not `0`. Token counts Faustus estimated are counted in `estimated_calls`.
`unaccounted_ms` is the run's wall clock minus the known phase time.
