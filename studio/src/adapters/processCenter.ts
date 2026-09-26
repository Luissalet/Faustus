import { ApiError, getJson } from './api';

/**
 * Process center — Studio side of `routes/process_center_routes.py` +
 * `src/process_center.py`: what is running because of Faustus (ports,
 * background jobs, launched profiles, MCP children, watched apps) and a
 * Stop for each row.
 *
 * Mirrors `adapters/connectors.ts`'s own `json`/`ok` helpers: `credentials:
 * 'same-origin'`, and a failed response throws `ApiError` with the server's
 * own detail rather than a generic message, so the screen can show why a
 * stop was refused instead of guessing.
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

/** `origin` as `src/process_center.py`'s `ProcRow` writes it. */
export type ProcOrigin = 'self' | 'faustus' | 'bg_job' | 'launch_profile' | 'connector' | 'ollama' | 'other';

export interface ProcRow {
  pid: number;
  name: string;
  created_at: number | null;
  origin: ProcOrigin;
  label: string;
  cmdline: string;
  cwd: string;
  exe: string;
  ports: number[];
  rss_mb: number | null;
  children: number;
  protected: boolean;
  protected_reason: string;
  uptime_s: number | null;
  parent_pid: number | null;
  /** The configured MCP server (McpServer.name) this row's cmdline matches
   * a `faustus`-origin child against, or null when it is not one — an
   * agent shell/tool child, or no server's command+args matched. */
  mcp_server: string | null;
}

export interface BgJobRow {
  id: string;
  pid: number | null;
  command: string;
  cwd: string;
  session_id: string;
  started_at: number | string | null;
}

export interface ProcessSnapshot {
  available: boolean;
  generated_at: number;
  ports: ProcRow[];
  faustus: ProcRow[];
  watched: ProcRow[];
  jobs: BgJobRow[];
}

export async function fetchProcesses(watched = true): Promise<ProcessSnapshot> {
  return getJson<ProcessSnapshot>(`/api/process-center?watched=${watched ? 1 : 0}`);
}

export interface StopResult {
  ok: boolean;
  code: string; // '' | gone | recycled | no_proof | self | os | protected | refused | ownership_unknown | unavailable | bad_pid
  reason: string;
  signalled: number[];
  refused: [number, string][];
  name?: string;
  port?: number;
  bg_job?: string;
}

export interface StopProcessInput {
  pid: number;
  created_at: number | null;
  allow_protected?: boolean;
}

export const stopProcess = (body: StopProcessInput) => post<StopResult>('/api/process-center/stop', body, 'process-center/stop');

export const stopPort = (port: number, allowProtected = false) =>
  post<StopResult>('/api/process-center/stop', { port, allow_protected: allowProtected }, 'process-center/stop');
