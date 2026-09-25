# Context tools: the model managing its own context

In a long agent run (hundreds of tool calls on a local model with a large
window) the mid-turn pressure pass (`apply_midturn_pressure`) keeps the
prompt under the soft ceiling with fixed rules: old or large tool results
spill to disk and history folds. Those rules cannot know which result the
model still needs. Five tools let the model decide that itself, on top of
the automatic pass. They add nothing to it and remove nothing from it.

- Module: `src/context_self_manage.py` (applies the operations)
- Handlers: `src/agent_tools/context_tools.py` (validate arguments, return
  an intent)
- Fenced-result offsets: `src/tool_result_segments.py`
- Loop wiring: `src/agent_loop.py` (`_apply_context_intent`, offer block
  after mid-turn pressure)

| Tool | Arguments | Effect |
|---|---|---|
| `context_status` | `top?` (1-30, default 8) | used / window tokens and %, the largest results still in context with their handles, pinned items, results already spilled (with overflow ids), model pins used / cap |
| `context_pin` | `handles?`, `snippet?` | the item's message is never folded by `compact_with_integrity` and never spilled by the mid-turn spill |
| `context_unpin` | `handles?`, `snippet?`, `all?` | removes pins; `all=true` removes every pin the model set in this session |
| `context_drop` | `handles` | each body goes to `context_overflow` and is replaced by the standard stub `[overflow id=<sha256> tool=… bytes=… call_id=… storage=…]`; `read_overflow` restores it |
| `context_note` | `handles`, `note` (≤ 4000 chars) | originals go to overflow; the first item's place holds ONE note (the model's text, one stub line per original, and the preserved block), the others become one-line stubs pointing at it |

No approval card: the tools share the `USER_INTERACTION` class with
`update_plan` / `plan_done` (they touch only the turn's own prompt, the
overflow store and the pin store under `DATA_DIR`).

## Handles

- Native function calling: the result's `tool_call_id` (`call_…`).
- Fenced-call routes (one "tool execution results" user message per round):
  `r<round>.<i>` for the i-th result of that round; `r<round>` names every
  result of the round.
- A fenced results message whose per-result offsets are unknown (for
  example history from before this change): `m<8 hex>`, the whole message.

`context_pin` / `context_unpin` also take `snippet`: a piece of text of at
least 12 characters that appears in exactly one non-system message. That
message is pinned, whatever its role.

## How an operation is applied

Handlers never see the conversation. Each returns
`{"context_intent": {"op": …, …}}`; right after the call, the loop's post-tool
hook passes the intent to `context_self_manage.apply_intent` together with the
live `messages`, rewrites the list in place, and replaces the tool result with
what actually happened (handles acted on, tokens freed, refusals and why).
The same hook runs on the main tool path and on the approved-tool replay.

It runs before the round's own results are appended, so a handle can only
name a result the model has already read. Never touched: the user's
messages, system messages, the assistant's own turns. Refused:

- a pinned item (unpin first; on a fenced route a pin covers the whole
  round's message, so its other results are refused too);
- a result that is already an overflow stub;
- by `context_drop`, a result that carries a pending approval id
  (`context_note` keeps those ids instead: approvals, sources and
  identifiers found in the originals are copied into the note with the same
  `Preserved (compaction-safe):` block compaction uses).

Overflow ids stay referenced by the stubs/note, so the overflow prune keeps
their blobs. In an incognito / no-memory run nothing is written to disk: the
stub says `storage=memory-only (incognito)` and the body cannot be
restored.

Pins go through `src/context_engine/compaction_pins.py` with the turn's owner
(`"system"` when there is none, the same owner `apply_midturn_pressure`
passes to compaction). A model pin's excerpt starts with `[model] `; the
model may hold at most `agent_context_tools_max_pins` of them per session
(user pins are not counted and not touched by `unpin all`).

## When the tools are offered

Prompt size matters on a local model, so the tools are not in every turn's
tool list. Each round, after mid-turn pressure, the loop offers them when:

- `agent_context_tools_enabled` is on, and
- usage ≥ `agent_context_tools_offer_pct` of the route's window, or the
  round number ≥ `agent_context_tools_offer_round`, or the user asked
  (a message naming a context tool, "free up your context", "context
  window", "libera el contexto", "ventana de contexto", …).

Once offered, they stay offered for the rest of the turn. A native route
gets their schemas; a fenced-call route, whose system prompt was built
before any offer, gets a one-time note with the call syntax. When usage
reaches the mid-turn soft ceiling (`agent_midturn_compact_pct`), one short
line reminds the model it can use them (once per turn).

| Setting | Default | Meaning |
|---|---|---|
| `agent_context_tools_enabled` | `true` | master switch; off = never offered, and a call is refused |
| `agent_context_tools_offer_pct` | `0.45` | usage (fraction of the window) from which they are offered |
| `agent_context_tools_offer_round` | `12` | round from which they are offered whatever the usage; `0` = usage only |
| `agent_context_tools_max_pins` | `20` | pins the model may hold per session |

## Fenced-mode spill

`spill_large_tool_results` used to spill only `role:"tool"` messages, so a
fenced-call route (the one local models without function calling use) never
spilled anything. The fenced results message now records each result's
offsets (`_tool_round`, `_tool_segments`: private keys, stripped before any
provider sees the message). The spill replaces old results (outside the last
`agent_midturn_keep_tool_rounds` rounds) or oversized ones
(≥ `agent_midturn_spill_chars`) one by one inside that message; results under
600 characters stay (their stub would not be smaller). A message whose
offsets are unknown or stale is spilled as one body. The untrusted wrapper
stays intact and every stub is reacquirable with `read_overflow`. Pinned
messages are skipped on both routes.

## Events and metrics

SSE `context_op` (catalogued in `docs/api/sse_events.json`), one per context
tool call:

```json
{"type": "context_op", "round": 14, "op": "drop", "ok": true,
 "handles": ["call_7f2"], "tokens_freed": 5728, "overflow_ids": ["3b1c…"]}
```

`metrics["harness"]` of the turn gains `total_tokens` and
`context_ops` = `{status, pins, unpins, drops, notes, tokens_freed}`.

Scorecard rows (`src/scorecard.py`) gain `input_tokens`, `total_tokens`,
`tokens_per_tool_call`, `tokens_per_round` (absent when the turn has no usage
figures) and `context_ops` when the model used any; `aggregate()` adds
`avg_input_tokens`, `avg_total_tokens`, `avg_tokens_per_tool_call` and
`avg_tokens_per_round` (means over the rows that carry them).
