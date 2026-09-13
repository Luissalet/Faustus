# faustus-sdk

A minimal TypeScript client for the Faustus chat wire: sessions, turns,
streaming, resume-by-cursor, cancellation, tool approvals and artifacts.
Works unmodified in CommonJS and ESM, in Node 18+ and in a modern browser.

No runtime dependencies, no peer dependencies — it only needs the global
`fetch`/`ReadableStream`/`FormData` that Node 18+ and every current browser
already provide.

## Install

```sh
npm install faustus-sdk
```

(From a local build instead of the registry: `cd sdk/ts && npm run build &&
npm pack`, then `npm install ../path/to/faustus-sdk-0.1.0.tgz` in the
consuming project — see `examples/` for two full, working installs, one ESM
and one CommonJS.)

## A 20-line example

```ts
import { FaustusClient } from 'faustus-sdk';

const client = new FaustusClient({
  baseUrl: process.env.FAUSTUS_URL!,
  token: process.env.FAUSTUS_TOKEN, // an `ody_…` token, scope `sessions`
});

const session = await client.sessions.create({ skipValidation: true });
const turn = await client.turns.create(session.id, { message: 'hello', mode: 'agent' });

for await (const event of turn) {
  if (event.type === 'delta') process.stdout.write(event.delta);
  if (event.type === 'ask_user' && event.data.kind === 'tool_approval') {
    await client.turns.decideToolApproval(session.id, {
      approvalId: event.data.approval_id!,
      decision: 'approve_task',
    });
  }
}

console.log('\n', (await turn.done).reason); // 'done'
```

## API

| Call | Wire | Notes |
| --- | --- | --- |
| `client.version()` | `GET /api/version` | server version, build, the client-adaptation notice |
| `client.sessions.create(input)` | `POST /api/session` | form-urlencoded |
| `client.sessions.list()` | `GET /api/sessions` | bare array |
| `client.sessions.update(id, patch)` | `PATCH /api/session/{id}` | see "Deviations" below |
| `client.sessions.remove(id)` | `DELETE /api/session/{id}` | |
| `client.turns.create(sid, input, opts?)` | `POST /api/chat_stream` | returns a `Turn` once headers arrive |
| `client.turns.resume(sid, {cursor}?)` | `GET /api/chat/resume/{sid}` | throws `RunNotActiveError` on 404 |
| `client.turns.stop(sid, {runId, scope}?)` | `POST /api/chat/stop/{sid}` | standalone stop, no `Turn` needed |
| `client.turns.answerQuestion(sid, input, opts?)` | `POST /api/chat_stream` | `question_id`/`option_ids`/`revision` |
| `client.turns.decideToolApproval(sid, input, opts?)` | `POST /api/chat_stream` | `tool_approval_id`/`tool_approval_decision` |
| `client.turns.status(sid)` | `GET /api/chat/stream_status/{sid}` | `null` on 404 |
| `client.questions.list()` | `GET /api/questions` | |
| `client.approvals.pending()` / `.active()` | `GET /api/approvals/pending`/`active` | |
| `client.artifacts.list(query?)` | `GET /api/artifacts` | |
| `client.artifacts.get(id)` / `.download(id)` / `.manifest(id)` / `.provenance(id)` | `GET /api/artifacts/{id}[/…]` | `download` returns `Uint8Array` |

`Turn` is an `AsyncIterable<SseEvent>` with `runId`, `replayed`,
`sessionId`, `lastSequence`, `cancel(scope?)` and a `done` promise that
resolves to `{reason, lastSequence, error?}` — `reason` is one of `'done'`
(saw `[DONE]`), `'stopped'`/`'run_gone'` (the server ended it), `'error'`
(the connection could not be recovered) or `'aborted'` (your own
`AbortSignal` fired).

## Reconnection, cancellation, idempotency

- **A dropped connection is recovered automatically**, by reconnecting to
  `GET /api/chat/resume/{sid}?cursor=<lastSequence>` — never by repeating
  the original `POST`, which has effects (a repeated `POST` would start a
  second turn). Events already delivered are never handed to you twice: a
  resumed stream can legitimately replay a few events you already saw (a
  superset, never a gap), and `Turn` drops anything at or below the highest
  `sequence` it already gave you.
- Silence for longer than `idleTimeoutMs` (default 60 000 ms — well above
  the server's 10 s heartbeat) is treated the same as a dropped connection.
- Up to `maxResumes` (default 5) reconnect attempts, with a 250 ms · 2ⁿ
  backoff between them. Exhausting them ends the turn with
  `{reason: 'error'}`; a 404 on resume (the run finished or died) ends it
  with `{reason: 'run_gone'}` — neither is thrown, both are read off
  `turn.done`.
- **Safe to retry:** reconnecting by cursor (`turns.resume`), checking
  status (`turns.status`), stopping (`turns.stop`/`turn.cancel`), listing
  anything. **Not safe to retry:** `turns.create` — call it once per turn;
  if you are not sure whether an earlier `POST` reached the server (a
  network error with no response at all), pass the *same*
  `clientMessageId` back into a fresh `turns.create()` call — the server
  recognises it and reconnects you to the turn it already started instead
  of running your message twice (`turn.replayed` is `true` when this
  happened; omit `clientMessageId` and the library mints one with
  `crypto.randomUUID()`).
- `turn.cancel(scope?)` calls `POST /api/chat/stop` using `turn.runId` as
  the fencing token (`X-Odysseus-Run-Id`) — without a known `runId` the
  server intentionally cancels nothing. It does not itself stop your
  iteration; the stream ends on its own once the server closes it.
  `scope: 'generation'` pauses (resumable); `'task'`/`'work'` really cancel,
  `'work'` also cancelling this session's background jobs.
- An `opts.signal` you pass aborts your *reading* of the turn
  (`{reason: 'aborted'}`) without calling stop server-side — the run keeps
  going; call `cancel()` if you actually want it stopped.

## Errors

Every failure is a typed subclass of `FaustusApiError` (`status`, `detail`,
`errorClass?`, `body?`) — `detail` is exactly what the server said (its
`detail` field, a string or `{message}`, then `error`, then `message`);
this library never guesses a cause from the message text.

- `UpgradeRequiredError` — 426: this build is older than the server's
  floor (`X-Faustus-Client-Version` too low).
- `QuestionConflictError` — 409 answering an `ask_user` question:
  `.reason` is `cancelled` | `stale_revision` | `already_answered` |
  `expired` | `not_found` (or a newer value this build doesn't know yet),
  `.questionId` the question it was about.
- `RunNotActiveError` — 404 from `turns.resume()`/`turns.status()`: no run
  is currently active for that session.
- `StreamAbortedError` — internal only; never thrown across this API (see
  `opts.signal` above).

## Event catalogue

`docs/api/sse_events.json` (in the main repository) is the versioned
source of truth. Every `stability: core` event has its own type here
(`ToolStartEvent`, `AskUserEvent`, `RunActivityEvent`, …, generated by
`scripts/gen-events.mjs` — `npm run check` fails the build if this
package's `src/events.generated.ts` has drifted from that file); an
`extended` event, or any type this build's catalogue doesn't know yet,
still arrives as `UnknownEvent` (`{type, ...rest}`) rather than being
dropped. `SCHEMA_VERSION` here always matches `src/api_version.API_VERSION`
on the server this build was written against.

## Getting a token

`POST /api/tokens` (form fields `name`, `scopes` or `profile`) already
exists in this tree (`routes/api_token_routes.py`) for the profiles it
knows about today. This package is written against a `sessions` scope
served through the `sdk` profile that a parallel change opens (see this
package's own build report for the exact wire it was written against) —
mint a token there once that profile exists, or request a token with the
`sessions` scope directly if your server already grants it by name. Either
way the client only needs `Authorization: Bearer ody_…`.

## Versioning

This build sends `X-Faustus-Client-Version: "2.0"` (`CLIENT_API_VERSION`)
on every request and reads `X-Faustus-Api-Version` back. See
`docs/api/versioning.md` (main repository) for the server's compatibility
policy — in short: an unversioned client is always accepted, a client
below the server's floor gets 426 (`UpgradeRequiredError`), and a
supported-but-older client gets a soft `client_adaptation_notice` back
from `GET /api/version` instead of a hard failure.

## A deliberate deviation

`client.sessions.update()` returns `SessionUpdateResult`, not a full
`Session`: `PATCH /api/session/{sid}` (`routes/session_routes.py::
rename_session`) echoes back only the fields it actually changed (`id`
plus whichever of `name`/`folder`/`model`/`endpoint_url`/`capabilities`/
`lost` you patched) — never `rag`/`archived`, which a full `Session` would
require. Returning a `Session` here would mean inventing values the server
never sent. See this package's build report for the exact line this was
read off.

## License

AGPL-3.0-or-later, same as the rest of this repository.
