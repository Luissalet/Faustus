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
pinned to the chat. The file name comes from `sha256(model, the server's model
file and chat template from /props, the tools, the exact system + document
text)`, so an edited document, another model or another template is another
state. A chat whose document changed loses its previous state.

1. **Before the request.** If the chat's slot already holds that state (the
   previous turn left it there) nothing is done (`warm`). Otherwise, if a file
   exists for it, `POST /slots/{id}?action=restore` loads it (`restored`).
2. **After the answer.** If the turn had to read the prefix (`miss`), the slot
   is saved with `POST /slots/{id}?action=save` (`saved`). The turn waits up
   to two seconds for it and lets it finish in the background beyond that
   (`pending`).

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

`status` is `restored` | `saved` | `warm` | `miss` | `off`. `off` carries
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
  state, or only those only that chat uses; `{removed, freed_bytes}`.

The MCP server `workers` exposes both as the tool `slot_cache`
(`action`: `status` | `clear`, optional `session_id`).
