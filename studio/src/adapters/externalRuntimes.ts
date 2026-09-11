import { ApiError, responseReason } from './api';

/**
 * CMP-06 (INFORME_COMPARATIVO_V2.md §3.5) — `/api/external-runtimes/herdr/*`
 * (`routes/external_runtimes_routes.py`, `src/external_runtimes/herdr.py`).
 *
 * READ-ONLY, on the backend and here: nothing in this file sends a session,
 * an input, or a command to Herdr — only status/config reads and the config
 * write for the connection itself (base_url/token). "Sin investigación
 * externa": the wire shape this talks to is documented as pending
 * validation against a real Herdr instance (docs/api/external_runtimes.md);
 * this adapter is only a thin, typed wrapper — no UI wiring here, that is a
 * later lote's job (see the ficha).
 */

export interface HerdrConfig {
  baseUrl: string;
  configured: boolean;
  /** Whether a token is currently stored — the token value itself is never returned. */
  tokenSet: boolean;
}

interface RawHerdrConfig {
  base_url?: string;
  configured?: boolean;
  token_set?: boolean;
}

function configFrom(raw: RawHerdrConfig): HerdrConfig {
  return {
    baseUrl: typeof raw.base_url === 'string' ? raw.base_url : '',
    configured: Boolean(raw.configured),
    tokenSet: Boolean(raw.token_set),
  };
}

export type HerdrCertainty = 'structured' | 'heuristic';

export interface HerdrPresence {
  sessionId: string;
  label: string;
  state: string;
  certainty: HerdrCertainty;
  /** Seconds since the presence signal was last confirmed, or null when unknown. */
  signalAgeS: number | null;
}

interface RawHerdrPresence {
  session_id?: string;
  label?: string;
  state?: string;
  certainty?: string;
  signal_age_s?: number | null;
}

function presenceFrom(raw: RawHerdrPresence): HerdrPresence {
  const certainty: HerdrCertainty = raw.certainty === 'structured' ? 'structured' : 'heuristic';
  return {
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : '',
    label: typeof raw.label === 'string' ? raw.label : '',
    state: typeof raw.state === 'string' ? raw.state : 'unknown',
    certainty,
    signalAgeS: typeof raw.signal_age_s === 'number' ? raw.signal_age_s : null,
  };
}

/** Same shape as `GitApiError`/`ModelRouterApiError`: `error_class` next to
 *  the message (`routes/git_routes.py::_error` convention). A `delivery`
 *  field (`'unknown' | 'not_delivered'`) is present only on a transport
 *  failure — mirrors `src/external_runtimes/herdr.py::TransportError`. */
export class ExternalRuntimesApiError extends ApiError {
  readonly errorClass: string | null;
  readonly delivery: 'unknown' | 'not_delivered' | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'ExternalRuntimesApiError';
    this.errorClass = errorClass;
    this.delivery = payload.delivery === 'unknown' || payload.delivery === 'not_delivered' ? payload.delivery : null;
    this.payload = payload;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    throw new ExternalRuntimesApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  return (await response.json()) as T;
}

/** `GET /api/external-runtimes/herdr/config` — admin only (mirrors
 *  `adapters/modelRouter.ts`'s posture for connection config). */
export async function getHerdrConfig(): Promise<HerdrConfig> {
  return configFrom(await request<RawHerdrConfig>('/api/external-runtimes/herdr/config'));
}

/** `PUT /api/external-runtimes/herdr/config` — `token` omitted keeps the
 *  stored token unchanged; `token: ''` clears it explicitly. */
export async function updateHerdrConfig(patch: { baseUrl: string; token?: string }): Promise<HerdrConfig> {
  const body: Record<string, unknown> = { base_url: patch.baseUrl };
  if (patch.token !== undefined) body.token = patch.token;
  return configFrom(
    await request<RawHerdrConfig>('/api/external-runtimes/herdr/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  );
}

/** `GET /api/external-runtimes/herdr/version` — any signed-in user. Throws
 *  `ExternalRuntimesApiError` with `errorClass === 'external_runtimes.not_configured'`
 *  when nothing is connected yet; callers should treat that as "not
 *  connected", not as an unexpected failure. */
export async function getHerdrVersion(): Promise<{ version: string }> {
  return request('/api/external-runtimes/herdr/version');
}

/** `GET /api/external-runtimes/herdr/sessions` — presence rows, each with
 *  its own `certainty`/`signalAgeS` rather than a single trust level for
 *  the whole list. */
export async function getHerdrSessions(): Promise<HerdrPresence[]> {
  const body = await request<{ sessions?: RawHerdrPresence[] }>('/api/external-runtimes/herdr/sessions');
  return (body.sessions ?? []).map(presenceFrom);
}
