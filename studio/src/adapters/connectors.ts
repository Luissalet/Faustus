import { ApiError, getJson } from './api';
import { t } from '../i18n';

/**
 * Connectors (CONTRATO_CONECTORES Lote F3, Studio side).
 *
 * This adapter is written against the F1/F2 contract described in
 * `docs/api/connectors.md` and `docs/api/tool_selection.md` — routes that do
 * not exist yet in THIS worktree (F1/F2 are separate lots, integrated by the
 * orchestrator later). Nothing here re-implements the catalogue, the health
 * checks or the launch profiles: it only calls the routes those lots own,
 * the same way `adapters/integrations.ts` only calls `/api/mcp/*` rather
 * than keeping its own copy of what an MCP server is.
 *
 * A route that is not there yet answers with a 404 the normal way; screens
 * degrade with a message (`ApiError`'s own text) rather than throwing an
 * unhandled rejection, so the rest of Studio keeps working while F1/F2 land.
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

/* ── Presets (F1.1) ── */

export interface ConnectorPreset {
  id: string;
  name: string;
  purpose: string;
  capabilities: string[];
  transport: string;
  /** Names the user has to fill (`JOBHUNT_DIR`, `APP_URL`…). Never a path of Luis's own machine. */
  placeholders: string[];
  defaults: Record<string, string>;
  app_url_default?: string | null;
  ui_url_default?: string | null;
  /** A documented example argv for a launch profile — never executed as-is. */
  launch_profile_hint?: Record<string, unknown> | null;
}

export async function listPresets(): Promise<ConnectorPreset[]> {
  const d = await getJson<ConnectorPreset[] | { presets?: ConnectorPreset[] }>('/api/connectors/presets');
  return Array.isArray(d) ? d : (d.presets ?? []);
}

/* ── Connectors (F1.3/F1.5) ── */

export type ConnectorState = 'unconfigured' | 'app_off' | 'connecting' | 'available' | 'error' | 'disabled' | 'unknown';

export interface ConnectorAppStatus {
  reachable: boolean | null;
  checked_at: string | null;
  latency_ms: number | null;
  detail: string;
}

export interface ConnectorAdapterStatus {
  mcp_status: string;
  tool_count: number | null;
  last_error: string | null;
}

export interface ConnectorStatus {
  state: ConnectorState;
  app: ConnectorAppStatus;
  adapter: ConnectorAdapterStatus;
  reasons: string[];
}

export interface ConnectorServerInfo {
  id: string;
  name: string;
  is_enabled: boolean;
  status: string;
  tool_count: number;
}

export interface Connector {
  id: string;
  preset_id: string | null;
  owner: string | null;
  /** Already redacted server-side (F1 principle 2): any key containing
   *  TOKEN/KEY/SECRET/PASSWORD arrives blanked out, never as a real value. */
  values: Record<string, string>;
  app_url: string | null;
  ui_url: string | null;
  launch_profile_id: string | null;
  preset: ConnectorPreset | null;
  server: ConnectorServerInfo;
  status: ConnectorStatus;
}

const STATE_LABEL: Record<ConnectorState, string> = {
  unconfigured: 'Not set up',
  app_off: 'App not running',
  connecting: 'Connecting…',
  available: 'Available',
  error: 'Error',
  disabled: 'Disabled',
  unknown: 'Unknown',
};

/** Icon-independent tone for the status chip; the icon itself is picked in
 *  the component (this file stays free of lucide-react/JSX). */
const STATE_TONE: Record<ConnectorState, 'ok' | 'warn' | 'bad' | 'muted'> = {
  unconfigured: 'muted',
  app_off: 'warn',
  connecting: 'warn',
  available: 'ok',
  error: 'bad',
  disabled: 'muted',
  unknown: 'muted',
};

export function stateLabel(state: ConnectorState): string {
  return t(STATE_LABEL[state] ?? state);
}

export function stateTone(state: ConnectorState): 'ok' | 'warn' | 'bad' | 'muted' {
  return STATE_TONE[state] ?? 'muted';
}

/** "App: responds · Adapter: 12 tools" — the two health signals the contract
 *  requires to stay visually separate (F1.3: app health ≠ adapter handshake). */
export function statusLine(status: ConnectorStatus): string {
  const app = status.app.reachable == null ? t('not checked') : status.app.reachable ? t('responds') : t('no response');
  const adapter = status.adapter.tool_count != null ? tCount(status.adapter.tool_count) : t(status.adapter.mcp_status || 'unknown');
  return t('App: {app} · Adapter: {adapter}', { app, adapter });
}
function tCount(n: number): string {
  return n === 1 ? t('{n} tool', { n }) : t('{n} tools', { n });
}

export async function listConnectors(check = false): Promise<Connector[]> {
  const q = check ? '?check=1' : '';
  const d = await getJson<Connector[] | { connectors?: Connector[] }>(`/api/connectors${q}`);
  return Array.isArray(d) ? d : (d.connectors ?? []);
}

export interface CreateConnectorInput {
  preset_id: string;
  values: Record<string, string>;
  name?: string;
  launch_profile_id?: string | null;
}
export const createConnector = (body: CreateConnectorInput) => post<Connector>('/api/connectors', body, 'connectors/create');

export interface UpdateConnectorInput {
  values?: Record<string, string>;
  name?: string;
  launch_profile_id?: string | null;
  is_enabled?: boolean;
}
export const updateConnector = (id: string, body: UpdateConnectorInput) => patch<Connector>(`/api/connectors/${encodeURIComponent(id)}`, body, 'connectors/update');

export const deleteConnector = (id: string) => del(`/api/connectors/${encodeURIComponent(id)}`, 'connectors/delete');

export const checkConnector = (id: string) => post<Connector>(`/api/connectors/${encodeURIComponent(id)}/check`, {}, 'connectors/check');
export const connectConnector = (id: string) => post<Connector>(`/api/connectors/${encodeURIComponent(id)}/connect`, {}, 'connectors/connect');
export const disconnectConnector = (id: string) => post<Connector>(`/api/connectors/${encodeURIComponent(id)}/disconnect`, {}, 'connectors/disconnect');

export interface ConnectorTool {
  name: string;
  description?: string;
  is_disabled?: boolean;
}
export const listConnectorTools = (id: string) => getJson<ConnectorTool[]>(`/api/connectors/${encodeURIComponent(id)}/tools`);

export interface LaunchResult {
  launched: boolean;
  already_running?: boolean;
  ready?: boolean;
  pid?: number | null;
  detail?: string | null;
}
export const launchConnector = (id: string) => post<LaunchResult>(`/api/connectors/${encodeURIComponent(id)}/launch`, {}, 'connectors/launch');

export type OpenResult =
  | { kind: 'url'; url: string }
  | { kind: 'exe'; launched: boolean; already_running?: boolean; pid?: number | null; detail?: string | null };
export const openConnector = (id: string) => post<OpenResult>(`/api/connectors/${encodeURIComponent(id)}/open`, {}, 'connectors/open');

/* ── Launch profiles (F1.4/F1.5) — user-authored only; the model never
 *  creates or edits one (contract principle 4). ── */

export interface LaunchReadiness {
  url?: string;
  timeout_s?: number;
  expect?: Record<string, string>;
}
export interface LaunchProfile {
  id: string;
  owner?: string | null;
  name: string;
  kind: 'process' | 'open_url' | 'open_exe';
  executable: string;
  argv: string[];
  cwd: string;
  env: Record<string, string>;
  readiness?: LaunchReadiness | null;
  created_at?: number | string | null;
  updated_at?: number | string | null;
}

export async function listLaunchProfiles(): Promise<LaunchProfile[]> {
  const d = await getJson<LaunchProfile[] | { profiles?: LaunchProfile[] }>('/api/launch-profiles');
  return Array.isArray(d) ? d : (d.profiles ?? []);
}

export interface LaunchProfileInput {
  name: string;
  kind: 'process' | 'open_url' | 'open_exe';
  executable: string;
  argv: string[];
  cwd: string;
  env: Record<string, string>;
  readiness?: LaunchReadiness | null;
}
export function saveLaunchProfile(id: string | null, body: LaunchProfileInput): Promise<LaunchProfile> {
  return id
    ? patch<LaunchProfile>(`/api/launch-profiles/${encodeURIComponent(id)}`, body, 'launch-profiles/update')
    : post<LaunchProfile>('/api/launch-profiles', body, 'launch-profiles/create');
}
export const deleteLaunchProfile = (id: string) => del(`/api/launch-profiles/${encodeURIComponent(id)}`, 'launch-profiles/delete');
