import { getJson, responseReason } from './api';

/**
 * Tool catalogue (TOOL-01, TOOL-03 partial) — one row per tool the backend
 * registry (`src/tool_registry.py`) can currently describe, native or fence
 * built-ins and live MCP tools alike. Read-only: `dryRunTool` only ever
 * validates arguments, it never runs anything.
 */

export interface McpToolState {
  server_id: string;
  status: string;
  raw_status?: string | null;
  error?: string | null;
}

export interface CatalogTool {
  name: string;
  version: string;
  description: string;
  effect_class: string;
  required_scopes: string[];
  timeout_ms: number;
  cancellation: string;
  idempotency: string;
  max_output_bytes: number;
  executor: string;
  mcp: McpToolState | null;
}

export interface CatalogListing {
  checked_at: string;
  fingerprint: string;
  count: number;
  tools: CatalogTool[];
}

export interface RetryPolicy {
  max_attempts: number;
  initial_delay_ms: number;
  retry_on: string[];
}

export interface ToolDescriptor extends CatalogTool {
  schema_version: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  retry_policy: RetryPolicy;
}

export interface CatalogEntry {
  checked_at: string;
  tool: ToolDescriptor;
  mcp: McpToolState | null;
}

export interface ArgumentIssue {
  field: string;
  kind: string;
  detail: string;
  seen?: unknown;
}

export interface ArgumentRepair {
  field: string;
  from: unknown;
  to: unknown;
  reason: string;
}

export interface DryRunResult {
  tool: string;
  schema_available: boolean;
  ok: boolean;
  errors: ArgumentIssue[];
  repairs: ArgumentRepair[];
  repaired_arguments: Record<string, unknown>;
  remaining_errors: ArgumentIssue[];
}

function qs(params: Record<string, string | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v as string)}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

/** Every tool this owner can currently reach, optionally narrowed by a
 * free-text query (name/description/effect/scope) and/or an exact executor
 * ('native' | 'fence' | 'mcp:<server_id>'). Most callers should instead fetch
 * once with no filter and narrow client-side — see ToolCatalogPanel — but
 * both server-side filters are exposed for a caller that wants a small
 * response (or a search that reaches beyond one page of client-side rows). */
export async function listToolCatalog(filter?: { q?: string; executor?: string }): Promise<CatalogListing> {
  return getJson<CatalogListing>(`/api/tools/catalog${qs({ q: filter?.q, executor: filter?.executor })}`);
}

export async function getToolDescriptor(name: string): Promise<CatalogEntry> {
  return getJson<CatalogEntry>(`/api/tools/catalog/${encodeURIComponent(name)}`);
}

/** Validate `arguments` against the tool's schema. Never executes the tool —
 * the backend route (`routes/tool_registry_routes.py`) only ever calls the
 * pure `validate_tool_arguments`/`repair_tool_arguments` helpers. */
export async function dryRunTool(name: string, args: Record<string, unknown>): Promise<DryRunResult> {
  const path = `/api/tools/catalog/${encodeURIComponent(name)}/dry-run`;
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ arguments: args }),
  });
  if (!response.ok) throw new Error(await responseReason(response, path));
  return (await response.json()) as DryRunResult;
}

/* ── Favorites: per-viewer only, never sent to the server ── */

const FAVORITES_KEY = 'faustus_tool_catalog_favorites';

export function loadFavoriteTools(): Set<string> {
  try {
    const raw = localStorage.getItem(FAVORITES_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

export function saveFavoriteTools(favorites: Set<string>): void {
  try {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify([...favorites]));
  } catch {
    /* private browsing / storage disabled: favourites just don't persist */
  }
}
