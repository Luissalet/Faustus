import { ApiError, getJson } from './api';

/**
 * Apps — the Processes screen's "Apps" section (docs contract: apps_contract.md
 * lot B, amended to live inside Processes rather than its own route).
 *
 * Talks to the SAME `/api/launch-profiles*` routes `adapters/connectors.ts`
 * already calls for CRUD (`listLaunchProfiles`, `saveLaunchProfile`,
 * `deleteLaunchProfile`, the `LaunchProfile`/`LaunchProfileInput` types —
 * imported from there rather than duplicated), plus the newer status/
 * stop/restart/open/icon/log routes those types did not yet cover, and the
 * `icon`/`open_url`/`stop_cmd`/`description`/`desktop` fields
 * `routes/connector_routes.py` + `src/launch_profiles.py` now accept.
 */

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

const post = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const patch = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const del = (path: string, what: string) => json<unknown>(path, { method: 'DELETE' }, what);

export type AppKind = 'process' | 'open_url' | 'open_exe';

export interface AppReadiness {
  url?: string;
  timeout_s?: number;
  expect?: Record<string, string>;
}

export interface AppStopCmd {
  executable: string;
  argv: string[];
  cwd?: string | null;
}

export interface AppProfile {
  id: string;
  owner?: string | null;
  name: string;
  kind: AppKind;
  executable: string;
  argv: string[];
  cwd: string;
  env: Record<string, string>;
  readiness?: AppReadiness | null;
  url?: string | null;
  icon?: string | null;
  open_url?: string | null;
  stop_cmd?: AppStopCmd | null;
  description?: string | null;
  desktop?: boolean;
  created_at?: number | string | null;
  updated_at?: number | string | null;
}

export interface AppProfileInput {
  name: string;
  kind: AppKind;
  executable: string;
  argv: string[];
  cwd: string;
  env: Record<string, string>;
  readiness?: AppReadiness | null;
  url?: string | null;
  icon?: string | null;
  open_url?: string | null;
  stop_cmd?: AppStopCmd | null;
  description?: string | null;
  desktop?: boolean;
}

export async function listApps(): Promise<AppProfile[]> {
  const d = await getJson<AppProfile[] | { profiles?: AppProfile[] }>('/api/launch-profiles');
  return Array.isArray(d) ? d : (d.profiles ?? []);
}

export function saveApp(id: string | null, body: AppProfileInput): Promise<AppProfile> {
  return id
    ? patch<AppProfile>(`/api/launch-profiles/${encodeURIComponent(id)}`, body, 'apps/update')
    : post<AppProfile>('/api/launch-profiles', body, 'apps/create');
}

export const deleteApp = (id: string) => del(`/api/launch-profiles/${encodeURIComponent(id)}`, 'apps/delete');

export type AppRunSource = 'owned' | 'port' | 'readiness' | 'none';

export interface AppStatus {
  running: boolean;
  source: AppRunSource;
  pid: number | null;
  created_at: number | null;
  port: number | null;
  ready: boolean;
  url: string | null;
  pid_command?: string | null;
  desktop_open?: boolean;
}

export async function appStatuses(): Promise<Record<string, AppStatus>> {
  const d = await getJson<{ statuses: Record<string, AppStatus> }>('/api/launch-profiles/status');
  return d.statuses ?? {};
}

export const appStatus = (id: string) => getJson<AppStatus>(`/api/launch-profiles/${encodeURIComponent(id)}/status`);

export interface AppLaunchResult {
  launched: boolean;
  already_running?: boolean;
  ready?: boolean;
  pid?: number | null;
  error?: string | null;
}

export interface AppStopResult {
  stopped: boolean;
  how?: 'stop_cmd' | 'owned' | 'port' | 'not_running';
  reason?: string | null;
}

export interface AppOpenResult {
  opened: boolean;
  already_open?: boolean;
  error?: string | null;
}

/** Lot A ships no standalone "launch a bare profile" route — only
 *  `/stop`, `/restart` and `/open` (the connector-scoped `/launch` needs a
 *  connector, which most Apps-screen profiles do not have). `restart()` is
 *  `stop()` (a no-op when nothing is running: `{stopped: false, how:
 *  "not_running"}`) followed by `launch()`, so calling it on an already
 *  stopped profile IS a clean start — that is what the Start button uses. */
export const startApp = (id: string) => post<AppLaunchResult & AppStopResult>(`/api/launch-profiles/${encodeURIComponent(id)}/restart`, {}, 'apps/start');

export const stopApp = (id: string) => post<AppStopResult>(`/api/launch-profiles/${encodeURIComponent(id)}/stop`, {}, 'apps/stop');
export const restartApp = (id: string) => post<AppLaunchResult & AppStopResult>(`/api/launch-profiles/${encodeURIComponent(id)}/restart`, {}, 'apps/restart');
export const openApp = (id: string) => post<AppOpenResult>(`/api/launch-profiles/${encodeURIComponent(id)}/open`, {}, 'apps/open');

export const appIconUrl = (id: string) => `/api/launch-profiles/${encodeURIComponent(id)}/icon`;

export async function appLog(id: string, lines = 300): Promise<string> {
  const d = await getJson<{ log: string }>(`/api/launch-profiles/${encodeURIComponent(id)}/log?lines=${lines}`);
  return d.log ?? '';
}
