/** Shared, dependency-free test doubles: no network, real `fetch`/`Response`/
 *  `ReadableStream` globals (Node ≥18), just no socket underneath them. */

export function textStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i]));
      i += 1;
    },
  });
}

/** A stream that sends `chunks`, then stays open forever without closing,
 *  erroring, or sending more — for idle-timeout tests. */
export function stallingStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
        return;
      }
      // Beyond the scripted chunks: never resolve `pull` again, so the
      // reader's next `read()` never settles on its own.
      return new Promise<void>(() => {});
    },
  });
}

/** A stream that sends `chunks`, then errors — simulating a dropped
 *  connection mid-turn. One chunk per `pull()` (not all enqueued up front
 *  in `start()`): erroring a stream drops whatever is still sitting
 *  unread in its internal queue, so chunks enqueued and then immediately
 *  errored in the same tick would never reach the reader at all. */
export function breakingStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
        return;
      }
      controller.error(new Error('connection reset'));
    },
  });
}

export function sseFrame(data: unknown, event?: string): string {
  return `${event ? `event: ${event}\n` : ''}data: ${JSON.stringify(data)}\n\n`;
}

export const SSE_DONE = 'data: [DONE]\n\n';

export function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

export function emptyResponse(status: number, headers: Record<string, string> = {}): Response {
  return new Response(null, { status, headers });
}

export function streamResponse(
  stream: ReadableStream<Uint8Array>,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  return new Response(stream, { status: init.status ?? 200, headers: { 'Content-Type': 'text/event-stream', ...init.headers } });
}

export interface FetchCall {
  url: string;
  method: string;
  init: RequestInit;
}

/** A minimal `fetch` double: routes to `handler` and records every call
 *  (URL, method, headers, body) so a test can assert on the request the
 *  client actually built, not just the response it got back. */
export function fakeFetch(handler: (call: FetchCall, callIndex: number) => Response | Promise<Response>): {
  fetch: typeof fetch;
  calls: FetchCall[];
} {
  const calls: FetchCall[] = [];
  const fn = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : (input as Request).url;
    const call: FetchCall = { url, method: init.method ?? 'GET', init };
    calls.push(call);
    return handler(call, calls.length - 1);
  }) as typeof fetch;
  return { fetch: fn, calls };
}

export function headerValue(init: RequestInit, name: string): string | undefined {
  const h = init.headers;
  if (!h) return undefined;
  if (h instanceof Headers) return h.get(name) ?? undefined;
  if (Array.isArray(h)) return h.find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1];
  return (h as Record<string, string>)[name] ?? (h as Record<string, string>)[name.toLowerCase()];
}
