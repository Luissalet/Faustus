import { ApiError } from './api';

/**
 * Home cards: automations pinned to the Home screen, showing their latest
 * result (`/api/home/cards`, routes/home_cards_routes.py + src/home_cards.py).
 *
 * A card is just a pinned task — "Run now" is the task's existing run
 * (`runAutomation` in adapters/automations.ts), removing the card does not
 * delete the task. Same fetch style as adapters/connectors.ts: same-origin
 * credentials, ApiError with the server's own detail text.
 */

export interface HomeCard {
  task_id: string;
  name: string;
  task_type: string | null;
  action: string | null;
  schedule: string | null;
  scheduled_time: string | null;
  timezone: string | null;
  cron_expression: string | null;
  status: string | null;
  next_run: string | null;
  last_run: string | null;
  result: string | null;
  result_at: string | null;
  result_model: string | null;
  last_status: 'success' | 'error' | 'running' | 'skipped' | null;
  last_error: string | null;
  running: boolean;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function ok(response: Response, what: string): Promise<Response> {
  if (response.ok) return response;
  let detail = '';
  try {
    const body = (await response.json()) as { detail?: unknown; error?: unknown };
    if (typeof body.detail === 'string') detail = body.detail;
    else if (typeof body.error === 'string') detail = body.error;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* not JSON */
  }
  throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
}

async function json<T>(path: string, init: RequestInit, what: string): Promise<T> {
  const r = await ok(await fetch(path, { credentials: 'same-origin', ...init }), what);
  const text = await r.text();
  return (text ? JSON.parse(text) : {}) as T;
}

const get = <T,>(path: string, what: string) => json<T>(path, { method: 'GET', headers: { Accept: 'application/json' } }, what);
const post = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const put = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const del = <T,>(path: string, what: string) => json<T>(path, { method: 'DELETE' }, what);

export async function listHomeCards(): Promise<HomeCard[]> {
  const d = await get<{ cards?: HomeCard[] }>('/api/home/cards', 'home/cards');
  return Array.isArray(d.cards) ? d.cards : [];
}

export async function pinHomeCard(taskId: string, position?: number): Promise<string[]> {
  const d = await post<{ cards?: string[] }>('/api/home/cards', { task_id: taskId, position }, 'home/cards/pin');
  return Array.isArray(d.cards) ? d.cards : [];
}

export async function unpinHomeCard(taskId: string): Promise<string[]> {
  const d = await del<{ cards?: string[] }>(`/api/home/cards/${encodeURIComponent(taskId)}`, 'home/cards/unpin');
  return Array.isArray(d.cards) ? d.cards : [];
}

export async function orderHomeCards(ids: string[]): Promise<string[]> {
  const d = await put<{ cards?: string[] }>('/api/home/cards/order', { order: ids }, 'home/cards/order');
  return Array.isArray(d.cards) ? d.cards : [];
}
