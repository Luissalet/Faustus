# Saved slots of pinned documents (`/api/engines/slot-cache`)

Faustus can keep a llama-server slot's KV state, with a document pinned to a
chat already read, in a file, and load it back on a later turn when the slot
lost it (another chat took it, the server restarted). The turn then does not
read the document again. Code: `src/slot_cache.py`.

It is **off by default** (`llamacpp_slot_cache`) because the server has to be
started with `--slot-save-path <folder>` and Faustus has to know the folder.
An engine started from Faustus gets the flag added to its profile when the
setting is on; for a server started by hand, point
`llamacpp_slot_cache_dir` at its folder.

## Settings

| key | default | meaning |
| --- | --- | --- |
| `llamacpp_slot_cache` | `false` | save and restore the slot of a pinned document |
| `llamacpp_slot_cache_max_tokens` | `8000` | largest system + pinned-document prefix (estimated tokens) worth a file; at least 256 is also required |
| `llamacpp_slot_cache_dir` | `""` | the server's `--slot-save-path`; empty = `<data>/slot_cache` |
| `llamacpp_slot_cache_max_mb` | `4096` | total size cap; the files used longest ago are deleted first |
| `llamacpp_slot_cache_max_age_days` | `14` | a file not restored for this long is deleted |

## When it acts

Only for a request that names a slot (`id_slot`, see `llamacpp_pin_session_slot`)
and whose prompt starts with the system prompt and at least one document
pinned to the chat. The file name comes from `sha256(model, the server's
identity, the tools, the exact system + document text)`, so an edited
document, another model or another server configuration is another state. A
chat whose document changed loses its previous state.

### What the server's identity covers

Read from `GET /props` on **every** turn (one small local request; only a
failure is remembered, for a minute), so a swapped model or a restart with
other flags is seen at once. It is the JSON of:

- the model file: the full `model_path`, plus its size and modification time
  when the file is visible from the machine Faustus runs on; for a server whose
  file is not visible, the path plus what `/v1/models` reports (`size`,
  `n_params`, `n_vocab`, `n_ctx_train`);
- a hash of the chat template;
- the context size (`default_generation_settings.n_ctx`, or `/slots` when
  `/props` does not carry it);
- `n_batch`, `n_ubatch`, `cache_type_k` and `cache_type_v` when the server
  exposes them (llama-server's `/props` does not always);
- `build_info` and `total_slots`.

Two servers that differ only in context size, or hold a file of the same name
in another folder, therefore never share a state. When the identity of a
server changes, what Faustus assumed about its slots is forgotten and the next
turn asks for a restore or reads the prompt. A restart with the same
configuration is not visible in `/props`: the server's own `cache_n` after a
restore or a warm turn tells (the turn is then a `miss`, and the saved file is
kept). A server that does not answer `/props` is identified by the model name
and prefix alone.

1. **Before the request.** If the chat's slot already holds that state (the
   previous turn left it there) nothing is done (`warm`). Otherwise, if a file
   exists for it, `POST /slots/{id}?action=restore` loads it (`restored`).
2. **After the answer.** If the turn had to read the prefix (`miss`), the slot
   is saved with `POST /slots/{id}?action=save`. The turn waits up to two
   seconds for the server's answer: `saved` once it is confirmed. If the
   server takes longer the turn is reported `saving`, never `saved`, and the
   save finishes in the background. How it ends is counted when it lands
   (`stats.saved` or `stats.errors`; `stats.saving` counts the turns that
   reported `saving`), the turn's record is updated when it is still held, and
   the chat's next turn carries `earlier_save: "failed"` or `"discarded"`.
   The next turn on the same slot waits (up to five seconds) for it.

It never blocks a turn: any error ends in the normal path and is logged once.
A server that answers HTTP 501 (no `--slot-save-path`) is not asked again for
ten minutes. If the server's own timings say it read the prefix again after a
restore (`cache_n` under half the prefix), the turn is reported as a miss and
the file is dropped and saved afresh.

## Turn metadata

The turn's `metrics` (the `metrics` stream event and the saved message
`metadata`) carry `prefix_cache` when the chat has a pinned document and the
setting is on:

```json
{"status": "restored", "slot": 2, "prefix_tokens": 1830, "restore_ms": 41.2,
 "prompt_n": 19, "cache_n": 1912, "prompt_ms": 12.4, "prefill_saved_ms": 940.5}
```

`status` is `restored` | `saved` | `saving` | `warm` | `miss` | `off`. `off` carries
`reason`: `unsupported` (server without a save path), `too_large`,
`too_small`. `prefill_saved_ms` estimates the prefill time the cache saved,
from the server's own prompt speed when the state was saved. An agent turn
with several rounds folds them into one record (`rounds`). Studio shows it as
the "Pinned document" row of "Why did it take this long?".

## Routes

Admin level, like `GET /api/engines/status`.

- `GET /api/engines/slot-cache` — `{enabled, dir, max_tokens, max_mb,
  max_age_days, entries: [{key, file, bytes, tokens, model, sessions, servers,
  created, last_used}], total_bytes, unsupported_servers, stats}`.
- `DELETE /api/engines/slot-cache[?session_id=...]` — delete every saved
  state, or only those only that chat uses; `{removed, freed_bytes,
  pending_saves}`. A save still running when this is called (`pending_saves`
  counts them, for that chat when one is named) records nothing when it lands
  and its file is removed again, unless another chat's state still shares it.

The MCP server `workers` exposes both as the tool `slot_cache`
(`action`: `status` | `clear`, optional `session_id`).
