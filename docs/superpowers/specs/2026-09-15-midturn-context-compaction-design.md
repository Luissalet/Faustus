# Mid-turn context compaction + disk overflow

**Date:** 2026-09-15  
**Status:** Approved in chat (option C); awaiting spec review before implementation plan  
**Problem source:** Silhouettes chat `c3a9e716-5115-407a-8934-bc0baf4a90d4` — ~1h47m, 61 rounds, context ~90%+, thrash on one failing test, ended in `rounds_exhausted`.

## Problem

Faustus already has turn-start compaction (`maybe_compact` at 85%, `compact_with_integrity`, `trim_for_context`). That is not enough for long **agentic** turns:

1. Compaction in `_build_route_request_state` runs when the route is built (and only when `defer_context_shaping or fallbacks`). Primary-route rounds after round 1 update `messages` with tool results but do **not** re-run LLM / integrity compaction.
2. Mid-turn bloat is dominated by **tool results** (full file writes, pytest dumps, repeated `write_file` bodies), not old user/assistant chat rows.
3. At ~90% of a ~200k window, local models spend minutes prefilling each round. The loop breaker fires; the step cap eventually kills the turn. From the UI this looks “stuck.”

Claude / ChatGPT-class agents compact **inside** a long tool loop. Faustus must do the same, and keep full spilled content on disk so detail is recoverable.

## Goals

- Prevent multi-hour mid-turn thrash caused by context pressure.
- Keep the live prompt under a **soft ceiling** (default ~70–75% of model context) for every model call in an agent turn.
- Spill folded tool bodies (and other large mid-turn spans) to disk with stable ids; leave stubs in the prompt that name how to recover.
- Preserve existing turn-start compaction, manual condense, and hard `trim_for_context` as outer layers.
- Fail soft: if compaction/spill fails, leave messages intact and fall through to current trim behavior.

## Non-goals

- Raising Ollama `num_ctx` / VRAM as the primary fix (may be a later optional knob).
- Changing loop-breaker / max-rounds policy (orthogonal; this fix should make hitting them for *context thrash* rare).
- Rewriting the Context Engine (`agent_context_engine`) live path.
- Making overflow browsable as a first-class Studio library feature in v1 (stubs + restore path are enough).

## Chosen approach

**Mid-turn pressure manager** with:

1. Tool-result (and large-message) **disk spill** → stubs in prompt  
2. Deterministic **`compact_with_integrity`**  
3. **`maybe_compact` / `summarize_rows`** only if still above the soft threshold  
4. Existing **`trim_for_context`** as last resort before the provider call  

Emit a `context_compacted` SSE event so the UI shows progress instead of silence.

## Architecture

```text
each agent round, before provider call
        │
        ▼
estimate_tokens(messages) / context_length
        │
        ├─ under soft threshold ──► trim_for_context (as today) ──► model
        │
        └─ at/over soft threshold
                │
                ▼
        spill_large_tool_results(...)     # disk overflow + stubs
                │
                ▼
        compact_with_integrity(...)       # deterministic fold
                │
                ▼
        still hot? ──yes──► maybe_compact / summarize_rows
                │
                ▼
        emit context_compacted SSE
                │
                ▼
        trim_for_context ──► model
```

### Soft vs hard thresholds

| Knob | Default | Role |
|------|---------|------|
| `agent_midturn_compact_pct` | `0.70` | Soft ceiling: start mid-turn pipeline |
| Existing `COMPACT_THRESHOLD` | `0.85` | Turn-start / LLM summary gate (unchanged semantics for call sites that already use it) |
| `trim_for_context` | hard `context_length - reserve` | Never send an over-budget prompt |

Mid-turn uses the soft setting. Turn-start paths keep using `COMPACT_THRESHOLD` unless we later unify (out of scope for v1).

### Spill store

- Location: `data/context_overflow/<session_id>/<sha256>.json` (under the app data dir, same conventions as other `data/` stores).
- Payload: `{schema_version, session_id, run_id, round, tool, call_id, role, content, created_at, content_sha256}`.
- Content addressed by hash so identical repeated `write_file` dumps share one blob.
- In-prompt stub (tool role preserved for provider pairing):

  ```text
  [overflow id=<sha256> tool=<name> bytes=<n> call_id=<id>]
  Full output spilled to disk. Re-read the workspace file if needed, or restore via overflow id.
  <short head: first ~400 chars or extracted paths/ids>
  ```

- Prune: files older than `agent_context_overflow_keep_hours` (default align with `agent_runs_keep_hours`, 48) and not referenced by the **current** in-memory message list for an active run.
- **Incognito / `no_memory`:** do not write overflow; fall back to in-memory truncation stubs only (no durable copy).

### What must never be spilled or summarized away

Same protections as CTX-02 / existing compactors:

- Latest user message  
- `ask_user` answers  
- Assistant/tool pairs from the most recent `agent_midturn_keep_tool_rounds` rounds (default **6**)  
- Messages marked essential (`metadata.compacted`, research primers, `_protected`)  
- User-attached images (existing `prune_tool_images` policy unchanged)

### Integration point (`src/agent_loop.py`)

Today, round > 1 sets `_active_route_state["messages"] = messages` and only re-trims when building the request. Change:

- Before building / trimming the primary route request each round, call a new async entrypoint, e.g. `await apply_midturn_pressure(messages, …)`.
- Replace `messages` with the returned list when anything changed.
- Yield `context_compacted` when spill/fold/summary ran.
- Keep `trim_for_context` afterward.

Do **not** require `defer_context_shaping or fallbacks` for this path — mid-turn pressure must run on the normal primary local route.

### SSE

```json
{
  "type": "context_compacted",
  "round": 12,
  "data": {
    "reason": "midturn_pressure",
    "tokens_before": 180000,
    "tokens_after": 120000,
    "context_length": 199680,
    "spilled": 8,
    "integrity_folded": true,
    "llm_summarized": false,
    "overflow_ids": ["abc…"]
  }
}
```

Studio may show a compact chip later; v1 only needs the event on the wire (and a test that it is emitted).

### Settings

| Key | Default | Meaning |
|-----|---------|---------|
| `agent_midturn_compact_enabled` | `true` | Master switch |
| `agent_midturn_compact_pct` | `0.70` | Soft threshold (0–1) |
| `agent_midturn_keep_tool_rounds` | `6` | Recent tool rounds kept full |
| `agent_midturn_spill_chars` | `8000` | Single message/result size that triggers spill even if recent |
| `agent_context_overflow_keep_hours` | `48` | Disk TTL |

Expose under Agent settings schema next to existing context / keep-images knobs.

## Testing

1. **Unit — spill:** Large tool message → stub in list + file on disk; reload by id restores content; identical content shares hash file.  
2. **Unit — pipeline order:** Under threshold → no-op; over threshold with fat old tools → spill first; integrity/LLM only if still hot (mock LLM).  
3. **Unit — protections:** Latest user / ask_user / last N tool rounds untouched.  
4. **Unit — incognito:** No files written; stubs still shorten prompt.  
5. **Agent loop:** Synthetic turn with many oversized tool results; after pressure, estimated tokens ≤ soft ceiling; `context_compacted` emitted; no dependence on `defer_context_shaping`.  
6. **Regression:** Existing `tests/test_context_compactor*.py`, `test_lote20_ctx02_*`, `test_condense.py` still pass.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Stub breaks tool_call / tool pairing | Stub keeps `role: tool` and `tool_call_id`; only `content` changes |
| Model can’t recover detail | Stub includes paths + overflow id; workspace files remain source of truth for writes |
| Disk fill | Content-hash dedupe + TTL prune |
| Extra latency from mid-turn LLM summary | Spill + integrity first; LLM only if still over soft threshold; prefer utility model via existing `summarize_rows` |
| Double-compaction fighting turn-start | Mid-turn is additive; turn-start unchanged; shared helpers only |

## Success criteria

- A long coding turn that previously climbed past ~90% and burned dozens of rounds rewriting the same file instead stays near the soft ceiling and keeps making progress (or fails for a real reason: tests, loop breaker on identical calls — not context thrash).  
- Overflow files exist for spilled tool bodies and are pruned after TTL.  
- Turning `agent_midturn_compact_enabled` off restores prior behavior.

## Implementation sketch (files)

| File | Change |
|------|--------|
| `src/context_overflow.py` (new) | Persist / load / prune overflow blobs |
| `src/context_compactor.py` | `apply_midturn_pressure`, spill stubs, wire integrity + maybe_compact |
| `src/agent_loop.py` | Call mid-turn pressure every round before trim; emit SSE |
| `src/settings.py` + `src/agent_settings_schema.py` | New settings |
| `tests/test_context_overflow.py` (new) | Spill store |
| `tests/test_midturn_pressure.py` (new) | Pipeline + loop hook |
| `FAUSTUS.md` | Short changelog note |

## Out of scope follow-ups

- UI overflow browser / one-click restore  
- Unifying soft and turn-start thresholds into one setting  
- Auto-raise `num_ctx` when VRAM allows  
- Teaching the model a dedicated `restore_overflow` tool (stubs + `read_file` on workspace paths first)
