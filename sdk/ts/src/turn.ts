/**
 * `Turn`: the live (or resumed) stream of one chat turn, with the
 * reconnect-by-cursor behaviour CONTRATO_SDK_S2.md § S2.2 describes. A
 * broken connection is always recovered with
 * `GET /api/chat/resume/{sid}?cursor=` — the original `POST` that started
 * the turn is never repeated (it has effects; resuming by cursor does not).
 */
import { FaustusApiError, RunNotActiveError, UpgradeRequiredError } from './errors.js';
import { HttpContext, RUN_ID_HEADER, describeFailure } from './http.js';
import { decode, pumpBody, type SseEvent } from './sse.js';
import type { StopResult, StopScope, StreamOptions, TurnEnd } from './types.js';

export const DEFAULT_IDLE_TIMEOUT_MS = 60_000;
export const DEFAULT_MAX_RESUMES = 5;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** A minimal pull-based async queue: `run()` pushes decoded events as they
 *  arrive; the consumer's `for await` pulls them. This decouples "the
 *  stream is being read" from "someone is iterating the turn right now" —
 *  `Turn.done` resolves the moment the underlying run ends, whether or not
 *  anything ever iterated it. */
class AsyncQueue<T> {
  private readonly items: T[] = [];
  private readonly waiting: ((result: IteratorResult<T>) => void)[] = [];
  private ended = false;

  push(item: T): void {
    if (this.ended) return;
    const waiter = this.waiting.shift();
    if (waiter) waiter({ value: item, done: false });
    else this.items.push(item);
  }

  end(): void {
    if (this.ended) return;
    this.ended = true;
    while (this.waiting.length) this.waiting.shift()!({ value: undefined, done: true });
  }

  private next(): Promise<IteratorResult<T>> {
    if (this.items.length > 0) {
      const value = this.items.shift() as T;
      return Promise.resolve({ value, done: false });
    }
    if (this.ended) return Promise.resolve({ value: undefined, done: true });
    return new Promise((resolve) => this.waiting.push(resolve));
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return { next: () => this.next() };
  }
}

export interface Turn extends AsyncIterable<SseEvent> {
  readonly runId: string | null;
  readonly replayed: boolean;
  readonly sessionId: string;
  readonly lastSequence: number;
  /** Cancels the run server-side. Uses `runId` as the fencing token
   *  (`X-Odysseus-Run-Id`) — without one known, the server fails closed and
   *  cancels nothing (a stale caller must not cancel a run it never started
   *  or resumed). Does not itself stop this `Turn`'s own iteration; the
   *  stream ends on its own once the server closes it. */
  cancel(scope?: StopScope): Promise<StopResult>;
  /** Resolves once this turn is over, for whatever reason — resolves even
   *  if nothing ever iterated the turn's events. */
  readonly done: Promise<TurnEnd>;
}

export interface TurnDeps {
  http: HttpContext;
  sessionId: string;
  runId: string | null;
  replayed: boolean;
  /** The sequence to resume from if this stream breaks — 0 for a fresh
   *  turn, or the caller's cursor for an explicit `turns.resume()`. */
  startSequence: number;
  options: StreamOptions;
}

export class TurnImpl implements Turn {
  readonly sessionId: string;
  runId: string | null;
  readonly replayed: boolean;
  lastSequence: number;
  readonly done: Promise<TurnEnd>;

  private readonly http: HttpContext;
  private readonly options: StreamOptions;
  private readonly queue = new AsyncQueue<SseEvent>();
  private resolveDone!: (end: TurnEnd) => void;
  private finished = false;

  constructor(deps: TurnDeps, initialBody: ReadableStream<Uint8Array>) {
    this.http = deps.http;
    this.sessionId = deps.sessionId;
    this.runId = deps.runId;
    this.replayed = deps.replayed;
    this.lastSequence = deps.startSequence;
    this.options = deps.options;
    this.done = new Promise<TurnEnd>((resolve) => {
      this.resolveDone = resolve;
    });
    void this.run(initialBody);
  }

  [Symbol.asyncIterator](): AsyncIterator<SseEvent> {
    return this.queue[Symbol.asyncIterator]();
  }

  async cancel(scope?: StopScope): Promise<StopResult> {
    return rawStop(this.http, this.sessionId, this.runId, scope);
  }

  private finish(end: TurnEnd): void {
    if (this.finished) return;
    this.finished = true;
    this.queue.end();
    this.resolveDone(end);
  }

  private onFrame(raw: Record<string, unknown>, sseEvent: string | null): void {
    const seq = typeof raw.sequence === 'number' && Number.isFinite(raw.sequence) ? raw.sequence : undefined;
    if (seq !== undefined) {
      // Resume replays a superset (never a gap) of what was already
      // delivered — skip anything at or below the highest sequence this
      // Turn has already handed out, so a reconnect never duplicates one.
      if (seq <= this.lastSequence) return;
      this.lastSequence = seq;
    }
    const event = decode(raw, sseEvent);
    if (event) this.queue.push(event);
  }

  private async run(initialBody: ReadableStream<Uint8Array>): Promise<void> {
    const idleTimeoutMs = this.options.idleTimeoutMs ?? DEFAULT_IDLE_TIMEOUT_MS;
    const maxResumes = this.options.maxResumes ?? DEFAULT_MAX_RESUMES;
    const signal = this.options.signal;

    let body: ReadableStream<Uint8Array> | null = initialBody;
    let resumeAttempts = 0;

    for (;;) {
      if (signal?.aborted) {
        this.finish({ reason: 'aborted', lastSequence: this.lastSequence });
        return;
      }

      if (body !== null) {
        try {
          await pumpBody(body, (raw, sseEvent) => this.onFrame(raw, sseEvent), { signal, idleTimeoutMs });
          this.finish({ reason: 'done', lastSequence: this.lastSequence });
          return;
        } catch (err) {
          if (signal?.aborted) {
            this.finish({ reason: 'aborted', lastSequence: this.lastSequence });
            return;
          }
          body = null; // fall through and try to reconnect
          void err; // the break reason itself isn't surfaced — see the doc comment on TurnEnd.error below
        }
      }

      // Never repeat the original POST — only ever reconnect by cursor.
      if (resumeAttempts >= maxResumes) {
        this.finish({ reason: 'error', lastSequence: this.lastSequence, error: new Error('Resume attempts exhausted.') });
        return;
      }
      const delayMs = 250 * 2 ** resumeAttempts;
      resumeAttempts += 1;
      await sleep(delayMs);
      if (signal?.aborted) {
        this.finish({ reason: 'aborted', lastSequence: this.lastSequence });
        return;
      }

      let response: Response;
      try {
        response = await rawResume(this.http, this.sessionId, this.lastSequence, signal);
      } catch {
        // Network failure reaching resume itself: this attempt is spent;
        // loop back (body stays null) and try again if attempts remain.
        continue;
      }
      if (response.status === 404) {
        this.finish({ reason: 'run_gone', lastSequence: this.lastSequence });
        return;
      }
      if (!response.ok || !response.body) {
        const info = await describeFailure(response);
        this.finish({
          reason: 'error',
          lastSequence: this.lastSequence,
          error: new FaustusApiError(info.message, response.status, info),
        });
        return;
      }
      this.runId = response.headers.get(RUN_ID_HEADER) ?? this.runId;
      body = response.body;
    }
  }
}

/** `GET /api/chat/resume/{sid}?cursor=` — shared by `Turn`'s own internal
 *  reconnect loop and the public `client.turns.resume()`. */
export async function rawResume(
  http: HttpContext,
  sessionId: string,
  cursor: number | undefined,
  signal?: AbortSignal,
): Promise<Response> {
  const qs = cursor != null ? `?cursor=${encodeURIComponent(String(cursor))}` : '';
  return http.request(`/api/chat/resume/${encodeURIComponent(sessionId)}${qs}`, {}, signal);
}

/** `client.turns.resume()`: unlike `Turn`'s own internal reconnect, a 404
 *  here throws `RunNotActiveError` — there is no turn in progress yet to
 *  end gracefully with `{reason: 'run_gone'}`. */
export async function publicResume(
  http: HttpContext,
  sessionId: string,
  options: { cursor?: number } & StreamOptions,
): Promise<Turn> {
  const response = await rawResume(http, sessionId, options.cursor, options.signal);
  if (response.status === 426) {
    const { detail, body } = await describeFailure(response);
    throw new UpgradeRequiredError(detail, body);
  }
  if (response.status === 404) throw new RunNotActiveError(sessionId);
  if (!response.ok || !response.body) {
    const info = await describeFailure(response);
    throw new FaustusApiError(info.message, response.status, info);
  }
  const runId = response.headers.get(RUN_ID_HEADER);
  return new TurnImpl(
    {
      http,
      sessionId,
      runId,
      replayed: false,
      startSequence: options.cursor ?? 0,
      options: { signal: options.signal, idleTimeoutMs: options.idleTimeoutMs, maxResumes: options.maxResumes },
    },
    response.body,
  );
}

/** `POST /api/chat/stop/{sid}` — shared by `Turn.cancel()` and the
 *  standalone `client.turns.stop()`. */
export async function rawStop(
  http: HttpContext,
  sessionId: string,
  runId: string | null | undefined,
  scope?: StopScope,
  signal?: AbortSignal,
): Promise<StopResult> {
  const headers: Record<string, string> = {};
  if (runId) headers[RUN_ID_HEADER] = runId;
  if (scope) headers['Content-Type'] = 'application/json';
  const response = await http.request(
    `/api/chat/stop/${encodeURIComponent(sessionId)}`,
    { method: 'POST', headers, body: scope ? JSON.stringify({ scope }) : undefined },
    signal,
  );
  if (!response.ok) {
    const info = await describeFailure(response);
    throw new FaustusApiError(info.message, response.status, info);
  }
  return (await response.json()) as StopResult;
}

/** `GET /api/chat/stream_status/{sid}` → `null` on 404 (nothing active),
 *  never throws for that case — matches `client.turns.status()`'s
 *  `Promise<StreamStatus | null>` signature. */
export async function rawStreamStatus(
  http: HttpContext,
  sessionId: string,
  signal?: AbortSignal,
): Promise<unknown | null> {
  const response = await http.request(`/api/chat/stream_status/${encodeURIComponent(sessionId)}`, {}, signal);
  if (response.status === 404) return null;
  if (!response.ok) {
    const info = await describeFailure(response);
    throw new FaustusApiError(info.message, response.status, info);
  }
  return response.json();
}
