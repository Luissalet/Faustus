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

/* ── Argument-level tool policy rules (src/tool_arg_policy.py) ──
 * Name-level policy (the enable/disable toggles above) can only say "this
 * tool is fine" or "this tool is blocked" — these rules add "fine, but only
 * with these arguments": a constraint on one dotted argument path of one
 * tool (exact name or a glob), enforced on every call before it executes. */

export type ToolArgRuleOp =
  | 'equals'
  | 'one_of'
  | 'prefix'
  | 'not_prefix'
  | 'regex'
  | 'max_len'
  | 'domain_in';

export type ToolArgRuleAction = 'deny' | 'ask';

export interface ToolArgRule {
  id: string;
  tool: string;
  arg: string;
  op: ToolArgRuleOp;
  value: unknown;
  action: ToolArgRuleAction;
  note: string;
}

export interface ToolArgRuleTestResult {
  allowed: boolean;
  action?: ToolArgRuleAction;
  rule_id?: string;
  arg?: string;
  op?: ToolArgRuleOp;
  value?: unknown;
  message?: string;
}

export async function listToolArgRules(): Promise<ToolArgRule[]> {
  const data = await getJson<{ rules: ToolArgRule[] }>('/api/tool-arg-rules');
  return data.rules;
}

/** Replaces the whole rule list — same shape as `setDisabledTools`: the
 * caller sends the full set it wants, not a delta. Throws with the server's
 * validation message (bad op, bad regex, ...) on a rejected PUT. */
export async function saveToolArgRules(rules: ToolArgRule[]): Promise<ToolArgRule[]> {
  const path = '/api/tool-arg-rules';
  const response = await fetch(path, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ rules }),
  });
  if (!response.ok) throw new Error(await responseReason(response, path));
  const data = (await response.json()) as { rules: ToolArgRule[] };
  return data.rules;
}

/** The Settings "try it" box: whether a tool+args pair would be allowed
 * under the currently SAVED rules (not whatever is still unsaved in the
 * editor) — same read-only intent as `dryRunTool`. */
export async function testToolArgRule(tool: string, args: Record<string, unknown>): Promise<ToolArgRuleTestResult> {
  const path = '/api/tool-arg-rules/test';
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ tool, args }),
  });
  if (!response.ok) throw new Error(await responseReason(response, path));
  return (await response.json()) as ToolArgRuleTestResult;
}
