/**
 * The low-level wire: headers, auth, the version negotiation header, and
 * reading a failure the way the server actually explained it — never by
 * guessing from the text (mirrors `studio/src/adapters/api.ts::
 * responseReason`, and never violates the same guard
 * `tests/test_studio_guards.py::test_no_substring_guessing_about_why_a_
 * request_failed` enforces there). CONTRATO_SDK_S2.md § S2.1/S2.2.
 */
import { FaustusApiError, UpgradeRequiredError } from './errors.js';
import type { ClientOptions, ServerVersion, Session, SessionSummary, SessionUpdateResult } from './types.js';
import { str } from './util.js';

export const CLIENT_API_VERSION = '2.0';
export const CLIENT_VERSION_HEADER = 'X-Faustus-Client-Version';
export const API_VERSION_HEADER = 'X-Faustus-Api-Version';
export const RUN_ID_HEADER = 'X-Odysseus-Run-Id';
export const IDEMPOTENT_REPLAY_HEADER = 'X-Faustus-Idempotent-Replay';

/** Combines any number of possibly-absent `AbortSignal`s into one that
 *  fires when the first of them does. Hand-rolled instead of
 *  `AbortSignal.any` (ES2023 / Node 20+) since this package's floor is
 *  Node 18 (`package.json#engines`). */
export function combineSignals(...signals: (AbortSignal | undefined)[]): AbortSignal | undefined {
  const present = signals.filter((s): s is AbortSignal => s != null);
  if (present.length === 0) return undefined;
  if (present.length === 1) return present[0];
  if (present.some((s) => s.aborted)) {
    const controller = new AbortController();
    controller.abort();
    return controller.signal;
  }
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  for (const s of present) s.addEventListener('abort', onAbort, { once: true });
  return controller.signal;
}

/** What the server actually said about a failed request — `detail` (a
 *  string, or `{message}`), then `error`, then `message`. Never a substring
 *  match against any of those; the caller reads `errorClass`/`detail` to
 *  branch, not the message text. */
export async function describeFailure(
  response: Response,
): Promise<{ message: string; detail: string; errorClass?: string; body?: unknown }> {
  let body: unknown;
  try {
    body = await response.clone().json();
  } catch {
    body = undefined;
  }
  let message = '';
  let errorClass: string | undefined;
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>;
    const detail = b.detail;
    if (typeof detail === 'string' && detail.trim()) {
      message = detail;
    } else if (detail && typeof detail === 'object') {
      const m = str((detail as Record<string, unknown>).message);
      if (m) message = m;
      const ec = str((detail as Record<string, unknown>).error_class);
      if (ec) errorClass = ec;
    }
    if (!message) message = str(b.error) ?? '';
    if (!message) message = str(b.message) ?? '';
    if (!errorClass) errorClass = str(b.error_class);
  }
  if (!message) message = `HTTP ${response.status}`;
  return { message, detail: message, errorClass, body };
}

export class HttpContext {
  readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly opts: ClientOptions;

  constructor(opts: ClientOptions) {
    this.opts = opts;
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    const impl = opts.fetch ?? globalThis.fetch;
    if (!impl) {
      throw new Error(
        'No fetch implementation available. Pass one via ClientOptions.fetch, or run on Node 18+ / a browser.',
      );
    }
    this.fetchImpl = impl;
  }

  url(path: string): string {
    return this.baseUrl + path;
  }

  /** Headers every request carries: auth (token wins over cookie — a
   *  token-authenticated request never also sends cookie credentials) and
   *  the negotiated client version. */
  authHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      [CLIENT_VERSION_HEADER]: this.opts.clientVersion ?? CLIENT_API_VERSION,
    };
    if (this.opts.token) {
      headers.Authorization = `Bearer ${this.opts.token}`;
    } else if (this.opts.cookie) {
      headers.Cookie = this.opts.cookie;
    }
    return headers;
  }

  /** A raw fetch against the server, with auth headers merged in and the
   *  configured default timeout combined with any caller-supplied signal.
   *  Returns the `Response` untouched — callers that need streaming bodies
   *  or custom status handling (chat_stream, resume, stop) use this
   *  directly; `requestJson` below is for everything else. */
  async request(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<Response> {
    const timeoutSignal = this.opts.defaultTimeoutMs != null ? AbortSignal.timeout(this.opts.defaultTimeoutMs) : undefined;
    const combined = combineSignals(signal, timeoutSignal);
    const headers = { ...this.authHeaders(), ...(init.headers as Record<string, string> | undefined) };
    return this.fetchImpl(this.url(path), {
      ...init,
      headers,
      credentials: this.opts.credentials,
      signal: combined,
    });
  }

  /** `request()`, then: 426 → `UpgradeRequiredError`; any other non-2xx →
   *  `FaustusApiError` built from `describeFailure`; otherwise the parsed
   *  JSON body. */
  async requestJson<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
    const response = await this.request(path, init, signal);
    if (response.status === 426) {
      const { detail, body } = await describeFailure(response);
      throw new UpgradeRequiredError(detail, body);
    }
    if (!response.ok) {
      const { message, detail, errorClass, body } = await describeFailure(response);
      throw new FaustusApiError(message, response.status, { detail, errorClass, body });
    }
    return (await response.json()) as T;
  }

  async version(signal?: AbortSignal): Promise<ServerVersion> {
    return this.requestJson<ServerVersion>('/api/version', { headers: { Accept: 'application/json' } }, signal);
  }

  async createSession(form: URLSearchParams, signal?: AbortSignal): Promise<Session> {
    return this.requestJson<Session>('/api/session', { method: 'POST', body: form }, signal);
  }

  async listSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
    return this.requestJson<SessionSummary[]>('/api/sessions', { headers: { Accept: 'application/json' } }, signal);
  }

  async updateSession(id: string, form: URLSearchParams, signal?: AbortSignal): Promise<SessionUpdateResult> {
    return this.requestJson<SessionUpdateResult>(`/api/session/${encodeURIComponent(id)}`, { method: 'PATCH', body: form }, signal);
  }

  async removeSession(id: string, signal?: AbortSignal): Promise<void> {
    await this.requestJson<unknown>(`/api/session/${encodeURIComponent(id)}`, { method: 'DELETE' }, signal);
  }
}
