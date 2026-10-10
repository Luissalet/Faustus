# Turn receipt on the Hub bus (`faustus.turn.completed`)

When Faustus persists the assistant reply of a turn it posts one event to the
family Hub's bus (`POST /api/events`, source `faustus`). It is a receipt of
what happened, never of what was said. Code: `src/turn_receipt.py`; hook:
`routes/chat_helpers.py::save_assistant_response` plus the two partial-reply
saves in `routes/chat_routes.py`. Tests: `tests/test_turn_receipt.py`.

## Payload

Only these keys ever leave Faustus (`turn_receipt.ALLOWED_KEYS`). Counters the
turn does not carry are left out, not guessed.

| Key | Meaning |
|---|---|
| `id` | `<session_id>:<message_id>` of the saved assistant message (stable) |
| `session_id` | the chat session |
| `ts` | epoch seconds when the receipt was built |
| `status` | `ok`, `error` (the model/agent failed) or `cancelled` (client left, partial reply saved) |
| `rounds`, `tool_calls` | agent rounds and tool calls of the turn |
| `input_tokens`, `output_tokens` | token counts as the turn measured or estimated them |
| `duration_s` | wall-clock seconds of the turn |
| `model` | the model that answered |
| `project_id` | the project the chat belongs to, when it has one |

Example (from a live test bus):

```json
{"id": "d32015a3-...:ce67a305-...", "session_id": "d32015a3-...", "ts": 1791656057.146,
 "status": "ok", "rounds": 1, "tool_calls": 0, "input_tokens": 10, "output_tokens": 5,
 "duration_s": 0.6, "model": "fake-coder"}
```

## Guarantees

- No prompt, reply, reasoning, chat title, file content or credential is ever included.
- Nothing is emitted for incognito turns.
- Once per saved reply; regenerations count as a reply.
- Fire-and-forget on a daemon thread with a 2 s ceiling; the turn never waits and a
  down Hub is not started or retried (the event is dropped; the chat database is the truth).
- Same Hub connection as the rest of Faustus (Hoard Hub connector, `HOARD_HUB_URL`,
  `HOARD_HUB_TOKEN_FILE`, the Hub's `url` file). `HOARD_EVENTS=0` turns family events off.
