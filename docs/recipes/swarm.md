# Swarm map: one instruction over many items

`swarm_map` applies the same instruction to a list of items (40 companies,
30 files, 100 URLs) in parallel, as many at once as the model server really
serves, and collects one result per item into a table. An optional reduce
step then makes one pass over all the results.

Code: `src/swarm/` (service, runner, capacity, lane, store, render),
`src/agent_tools/swarm_tools.py`, `routes/swarm_routes.py`,
`studio/src/screens/agents/Swarm.tsx`. Tests: `tests/test_swarm.py`.

## Swarm, delegate or fan-out

| You have | Use |
|---|---|
| Many items, the same instruction for each ("for each of these 40 companies, find the sector and size") | `swarm_map` |
| A handful (up to 4) of different tasks that together make one job, usually in one code folder | `delegate_agents` |
| One prompt and several models, and you want to see which does it best | `fanout_run` |

The swarm keeps every answer; fan-out keeps the best one. A swarm item is
small and independent: if item 12 depends on item 11, it is not a swarm.

## Using it

```json
{"instruction": "Company: {item.name}. Give its sector and approximate headcount.",
 "items": [{"name": "Acme"}, {"name": "Globex"}],
 "output_fields": ["sector", "headcount"],
 "reduce": "Group the companies by sector and name the three largest.",
 "wait": true}
```

* `instruction` — `{item}` is replaced by the item (JSON for an object),
  `{item.key}` by one key of it. Without a placeholder the item is appended.
* `items` — strings or small JSON objects (at most 8000 characters each once
  rendered; pass a path or URL instead of a whole document).
* `mode` — `llm` (default): one model call per item, no tools. `agent`: one
  limited worker per item (the `delegate_agents` worker, default 6 rounds,
  at most 12), optionally restricted to `tools`. The worker inherits every
  restriction on the caller and can never start another swarm, delegation
  or fan-out; a caller that is itself a worker at the depth ceiling
  (`agent_subagent_depth`) is refused. A worker that finishes cleanly leaves
  no chat behind; a failed one keeps its chat for inspection.
* `output_fields` — each answer is asked as a JSON object with these keys
  (sent as a response schema where the backend supports one) and becomes a
  row; a reply without that object is a failed attempt.
* `reduce` — one final call over the collected results. When the results do
  not fit in one prompt they are reduced in chunks, then the chunk answers
  are combined.
* `wait` / `wait_timeout` — for small jobs: wait up to `wait_timeout`
  seconds (default 120, max 900) and return the table inline. Otherwise the
  tool returns a `run_id` at once and the run carries on in the background;
  follow it with `swarm_status`, page the rows with `swarm_results`, stop it
  with `swarm_cancel`, continue it with `swarm_map` `resume_run_id`.
* `model` / `endpoint_id` — default is the chat's own model; without a chat,
  the task model.
* `per_item_timeout` (default 180 s llm, 900 s agent), `max_parallel` (only
  lowers), `max_items` (only lowers).

Each item gets one retry. An item that fails twice (error, timeout, empty
reply, missing fields) is recorded as failed with its error and the run goes
on. The final state is `done` (all ok), `partial`, `failed` (none ok) or
`cancelled`.

## What comes out

`DATA_DIR/swarm/<run_id>/`:

* `manifest.json` — the spec, status, counts, the parallelism used and why.
* `items.json` — the items as given.
* `results.jsonl` — one line per finished item, appended as it finishes. An
  item without a line is pending: after a crash, a restart or a cancel, a
  resume runs exactly those. A run whose manifest says running but that no
  process is running shows as `interrupted`.
* `exports/<run_id>.md`, `.csv`, `.jsonl` (and `<run_id>-reduce.md`) — the
  table, also recorded in the artifact store under the chat's session.

REST (owner-scoped; another owner's run is a 404): `GET /api/swarm`,
`GET /api/swarm/{id}`, `GET /api/swarm/{id}/results?offset&limit&status`,
`POST /api/swarm/{id}/cancel`, `POST /api/swarm/{id}/resume`,
`GET /api/swarm/{id}/files/{name}`. The Workers screen lists the runs with a
progress bar, ok/failed counts and download links.

While `wait` is on, the tool reports progress as `tool_progress` events with
`event: "swarm_progress"` and a `swarm` object (`total`, `ok`, `failed`,
`pending`).

## How many items run at once

| Backend | Parallelism |
|---|---|
| llama-server on this machine or the LAN | its own slots: `/slots` (one entry per slot), or `total_slots` from `/props` when `/slots` is off; read once a minute |
| Ollama | setting `swarm_ollama_parallel` (default 1) |
| Remote API | setting `swarm_api_parallel` (default 8) |
| Any other local server | 1 |

Whatever the source, it is capped by `swarm_max_parallel` (default 16) and by
the number of pending items. One run takes at most `swarm_max_items` items
(default 200); a longer list is refused, not truncated.

### The chat keeps priority

Faustus normally sends every call to a local server through one lock (most
local servers have one generation pipe). A swarm item calling the server it
was sized for skips that lock, since its own limit already matches the
server's slots, but it does not start while a foreground call is waiting for
the model or holding it. The effect: while you chat, no new swarm call
starts; the ones already running finish. Calls to any other server go
through the usual lock.

## Giving llama-server parallel slots

`-np N` (`--parallel N`) makes N slots. The context given with `-c` is split
between them: `-c 235000 -np 4` gives each slot about 58,750 tokens
(235000 / 4). Each swarm item runs in one slot, so the per-slot context is
the limit for one item's prompt plus its answer (for agent mode, for the
whole worker conversation).

VRAM: the KV cache is sized by the total `-c`, not by the number of slots.
Splitting the same `-c` into more slots costs almost no extra memory (a
little more compute buffer); it only shortens each slot's context. Keeping
each slot at the old context means multiplying `-c` by N, and the KV cache
grows by the same factor, which is what usually does not fit. A quantized
KV cache (`--cache-type-k q8_0 --cache-type-v q8_0`, needs flash attention)
roughly halves that memory. Recent llama.cpp builds also have
`--kv-unified`, where the slots share one pool instead of fixed equal parts;
check `llama-server --help` on your build.

Throughput: N slots do not make one answer faster; they let N answers be
generated together, so a batch finishes sooner while each item may take a
bit longer than it would alone.

## Giving Ollama parallel requests

Ollama reads `OLLAMA_NUM_PARALLEL` when the Ollama server starts, and no
request can read it back, so Faustus cannot detect it. Start Ollama with
it (for example `OLLAMA_NUM_PARALLEL=4`) and set `swarm_ollama_parallel` to
the same number. Ollama allocates the context for each parallel request, so
the KV memory for a model is roughly `num_ctx × OLLAMA_NUM_PARALLEL`: four
parallel requests at 32k context take about the KV memory of one at 128k.
If that no longer fits in VRAM, Ollama offloads layers to the CPU and every
request gets slower, which defeats the purpose; lower `num_ctx` for that
model or the parallel count.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `swarm_ollama_parallel` | 1 | parallel requests the Ollama server was started with (`OLLAMA_NUM_PARALLEL`) |
| `swarm_api_parallel` | 8 | calls at once to a remote API |
| `swarm_max_parallel` | 16 | ceiling for any backend |
| `swarm_max_items` | 200 | most items in one run |
