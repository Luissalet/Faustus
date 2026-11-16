import { ApiError, getJson, responseReason } from './api';

/**
 * Studio side of the agent-setup routes: named delegation routes
 * (`/api/delegation-routes`), the importer of other coding agents' setups
 * (`/api/agent-import/*`), Spotify (`/api/spotify/*`) and the X self-chat
 * bridge (`/api/chat-bridges/xchat/*`).
 */

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

export interface DelegationRoute {
  name: string;
  description: string;
  model: string;
  endpoint_id: string;
  effort: string;
  agent: string;
  category: string;
  skills: string[];
}

export interface DelegationRoutes {
  routes: DelegationRoute[];
  default: string;
  schema: { enum: string[]; description: string } | null;
}

export const getDelegationRoutes = () => getJson<DelegationRoutes>('/api/delegation-routes');
export const saveDelegationRoutes = (routes: DelegationRoute[], def: string) =>
  send<DelegationRoutes>('/api/delegation-routes', 'PUT', { routes, default: def });

export type ImportAgent = 'claude-code' | 'codex' | 'cursor';

export interface ImportItem {
  key: string;
  agent: ImportAgent;
  kind: 'instructions' | 'memory' | 'mcp' | 'skill' | 'agent';
  name: string;
  source: string;
  summary: string;
  status: 'new' | 'changed' | 'unchanged' | 'exists';
  warnings: string[];
}

export interface ImportSource {
  agent: ImportAgent;
  root: string;
  found: boolean;
  items: ImportItem[];
  skipped: string[];
}

export interface ImportResult {
  imported?: { key: string; targets: string[]; replaced: boolean }[];
  failed?: { key: string; error: string }[];
  skipped?: string[];
  would_import?: ImportItem[];
  agents?: string[];
}

export const previewImport = (agents: ImportAgent[]) =>
  getJson<{ sources: ImportSource[] }>(`/api/agent-import/preview?agents=${encodeURIComponent(agents.join(','))}`);
export const applyImport = (agents: ImportAgent[], keys: string[]) =>
  send<ImportResult>('/api/agent-import/apply', 'POST', { agents, keys });
export const resyncImport = (dryRun = false) => send<ImportResult>('/api/agent-import/resync', 'POST', { dry_run: dryRun });

export interface SpotifyStatus {
  configured: boolean;
  logged_in: boolean;
  redirect_uri: string;
  login_url: string;
  scope: string;
  setup?: string;
}

export const spotifyStatus = () => getJson<SpotifyStatus>('/api/spotify/status');
export const spotifyLogout = () => send<{ ok: boolean }>('/api/spotify/logout', 'POST');

export interface XChatStatus {
  enabled: boolean;
  running: boolean;
  configured: boolean;
  missing: string[];
  xdk_installed: boolean;
  user_id: string;
  last_error: string;
  messages_answered: number;
}

export const xchatStatus = () => getJson<XChatStatus>('/api/chat-bridges/xchat/status');
export const xchatSetup = () => send<{ ok: boolean; user_id: string; username?: string; registered: boolean }>('/api/chat-bridges/xchat/setup', 'POST');
