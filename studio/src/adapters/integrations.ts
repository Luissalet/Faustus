import { getJson } from './api';

/**
 * Integrations — every external connection in one list, as the previous
 * interface's unified Integrations tab (static/js/settings.js
 * `initUnifiedIntegrations`): API keys with presets, CalDAV accounts,
 * contacts (import + CardDAV), mail accounts, MCP servers, the Codex and
 * Claude agent tokens, and the Bitwarden vault. Same routes, same bodies.
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: unknown; error?: string };
    if (typeof d.detail === 'string') msg = d.detail;
    else if (d.error) msg = d.error;
    else if (d.detail) msg = JSON.stringify(d.detail);
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

async function json<T>(path: string, init: RequestInit, what: string): Promise<T> {
  const r = await ok(await fetch(path, { credentials: 'same-origin', ...init }), what);
  const text = await r.text();
  return (text ? JSON.parse(text) : {}) as T;
}

const post = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const put = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const patch = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);
const del = (path: string, what: string) => json<unknown>(path, { method: 'DELETE' }, what);
function form(fields: Record<string, string | undefined>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) fd.append(k, v);
  return fd;
}

export type IntegrationKind = 'api' | 'caldav' | 'contacts' | 'carddav' | 'email' | 'mcp' | 'codex' | 'claude' | 'vault';

export const KIND_LABEL: Record<IntegrationKind, string> = {
  api: 'API',
  caldav: 'CalDAV',
  contacts: 'Contacts',
  carddav: 'CardDAV',
  email: 'Mail',
  mcp: 'MCP',
  codex: 'Codex',
  claude: 'Claude',
  vault: 'Vault',
};

/* ── API integrations ── */

export interface ApiIntegration {
  id: string;
  name: string;
  base_url?: string;
  auth_type?: string;
  auth_header?: string;
  enabled?: boolean;
  preset?: string;
  has_key?: boolean;
}
export interface ApiPreset {
  name: string;
  base_url?: string;
  auth_type?: string;
  auth_header?: string;
  hint?: string;
}

export async function listApiIntegrations(): Promise<ApiIntegration[]> {
  return (await getJson<{ integrations?: ApiIntegration[] }>('/api/auth/integrations')).integrations ?? [];
}
export async function apiPresets(): Promise<Record<string, ApiPreset>> {
  return (await getJson<{ presets?: Record<string, ApiPreset> }>('/api/auth/integrations/presets')).presets ?? {};
}
export async function saveApiIntegration(id: string | null, body: { name: string; base_url: string; auth_type: string; auth_header: string; preset?: string; api_key?: string }): Promise<string> {
  const d = id ? await put<{ id?: string; integration?: { id: string } }>(`/api/auth/integrations/${id}`, body, 'integration') : await post<{ id?: string; integration?: { id: string } }>('/api/auth/integrations', body, 'integration');
  return id ?? d.integration?.id ?? d.id ?? '';
}
export const deleteApiIntegration = (id: string) => del(`/api/auth/integrations/${id}`, 'integration');
export async function testApiIntegration(id: string): Promise<{ ok: boolean; message?: string }> {
  return json<{ ok: boolean; message?: string }>(`/api/auth/integrations/${id}/test`, { method: 'POST' }, 'integration/test');
}

/* ── CalDAV accounts ── */

export interface CalDavAccount {
  id: string;
  label?: string;
  url: string;
  username?: string;
}
export async function listCalDav(): Promise<CalDavAccount[]> {
  return (await getJson<{ accounts?: CalDavAccount[] }>('/api/calendar/config/accounts')).accounts ?? [];
}
export function saveCalDav(id: string | null, body: { label: string; url: string; username: string; password?: string }) {
  return id ? put(`/api/calendar/config/accounts/${id}`, body, 'caldav') : post('/api/calendar/config/accounts', body, 'caldav');
}
export const deleteCalDav = (id: string) => del(`/api/calendar/config/accounts/${id}`, 'caldav');
export async function testCalDav(body: { url: string; username: string; password?: string; account_id?: string }): Promise<{ ok?: boolean; success?: boolean; message?: string; error?: string; calendars?: unknown[] }> {
  const r = await fetch('/api/calendar/test', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(body) });
  const d = (await r.json().catch(() => ({}))) as { ok?: boolean; success?: boolean; message?: string; error?: string; detail?: string; calendars?: unknown[] };
  return { ...d, ok: r.ok && (d.ok ?? d.success ?? true), message: d.message ?? d.error ?? d.detail };
}

/* ── contacts ── */

export interface Contact {
  uid: string;
  name?: string;
  emails?: string[];
  phones?: string[];
  address?: string;
}
export interface ContactsConfig {
  url?: string;
  carddav_url?: string;
  username?: string;
  carddav_username?: string;
  password?: boolean | string;
}
export const contactsConfig = () => getJson<ContactsConfig>('/api/contacts/config');
export async function listContacts(): Promise<{ contacts: Contact[]; count: number }> {
  const d = await getJson<{ contacts?: Contact[]; count?: number }>('/api/contacts/list');
  return { contacts: d.contacts ?? [], count: Number(d.count ?? d.contacts?.length ?? 0) };
}
export const saveCardDav = (body: { carddav_url: string; carddav_username: string; carddav_password?: string }) => put('/api/contacts/config', body, 'carddav');
export const clearContacts = () => del('/api/contacts/clear', 'contacts/clear');
export const addContact = (body: { name: string; email: string; phone: string; address: string }) => post('/api/contacts/add', body, 'contacts/add');
export const updateContact = (uid: string, body: { name: string; emails: string[]; phones: string[]; address: string }) => put(`/api/contacts/${encodeURIComponent(uid)}`, body, 'contact');
export const deleteContact = (uid: string) => del(`/api/contacts/${encodeURIComponent(uid)}`, 'contact');
export async function importContacts(body: { vcf?: string; csv?: string }): Promise<{ imported: number; total: number; failed: number }> {
  const d = await post<{ imported?: number; total?: number; failed?: number }>('/api/contacts/import', body, 'contacts/import');
  return { imported: Number(d.imported ?? 0), total: Number(d.total ?? 0), failed: Number(d.failed ?? 0) };
}
export async function exportContacts(format: 'vcf' | 'csv'): Promise<void> {
  const r = await ok(await fetch(`/api/contacts/export?format=${format}`, { credentials: 'same-origin' }), 'contacts/export');
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = format === 'csv' ? 'faustus-contacts.csv' : 'faustus-contacts.vcf';
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ── mail accounts ── */

export interface EmailAccount {
  id: string;
  name: string;
  from_address?: string;
  display_name?: string;
  imap_host?: string;
  imap_port?: number;
  imap_user?: string;
  imap_starttls?: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: string;
  smtp_user?: string;
  is_default?: boolean;
  enabled?: boolean;
  oauth_provider?: string | null;
  provider?: string;
}
export interface EmailBody {
  name: string;
  from_address: string;
  display_name: string;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  imap_starttls: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_security: string;
  smtp_user: string;
  is_default: boolean;
  imap_password?: string;
  smtp_password?: string;
  account_id?: string;
}
export const EMAIL_PROVIDERS: Record<string, { label: string; emailEx: string; imap: { host: string; port: number; starttls: boolean }; smtp: { host: string; port: number }; oauth?: 'google'; note?: string }> = {
  gmail: { label: 'Gmail', emailEx: 'you@gmail.com', imap: { host: 'imap.gmail.com', port: 993, starttls: false }, smtp: { host: 'smtp.gmail.com', port: 465 }, note: 'Gmail needs an App Password (Google account → Security → 2-Step Verification → App passwords), not your normal password.' },
  google_workspace: { label: 'Google Workspace / .edu', emailEx: 'you@yourschool.edu', imap: { host: 'imap.gmail.com', port: 993, starttls: false }, smtp: { host: 'smtp.gmail.com', port: 587 }, oauth: 'google' },
  migadu: { label: 'Migadu', emailEx: 'you@yourdomain.com', imap: { host: 'imap.migadu.com', port: 993, starttls: false }, smtp: { host: 'smtp.migadu.com', port: 465 } },
  icloud: { label: 'iCloud', emailEx: 'you@icloud.com', imap: { host: 'imap.mail.me.com', port: 993, starttls: false }, smtp: { host: 'smtp.mail.me.com', port: 587 }, note: 'iCloud needs an app-specific password from appleid.apple.com.' },
  outlook: { label: 'Outlook / Office 365', emailEx: 'you@outlook.com', imap: { host: 'outlook.office365.com', port: 993, starttls: false }, smtp: { host: 'smtp.office365.com', port: 587 }, note: 'Microsoft disables password login for IMAP/SMTP in most accounts and Faustus does not do Microsoft OAuth yet, so this preset only works where basic auth is still on.' },
  fastmail: { label: 'Fastmail', emailEx: 'you@fastmail.com', imap: { host: 'imap.fastmail.com', port: 993, starttls: false }, smtp: { host: 'smtp.fastmail.com', port: 465 }, note: 'Fastmail needs an app password (Settings → Privacy & Security → App passwords).' },
  yahoo: { label: 'Yahoo', emailEx: 'you@yahoo.com', imap: { host: 'imap.mail.yahoo.com', port: 993, starttls: false }, smtp: { host: 'smtp.mail.yahoo.com', port: 465 }, note: 'Yahoo needs an app password (Account security → Generate app password).' },
  dovecot: { label: 'Dovecot IMAP (no SMTP)', emailEx: 'you@example.com', imap: { host: '', port: 31143, starttls: false }, smtp: { host: '', port: 465 } },
};
export async function listEmailAccounts(): Promise<EmailAccount[]> {
  return (await getJson<{ accounts?: EmailAccount[] }>('/api/email/accounts')).accounts ?? [];
}
export async function saveEmailAccount(id: string | null, body: EmailBody): Promise<string> {
  const d = id ? await put<{ ok?: boolean; id?: string }>(`/api/email/accounts/${id}`, body, 'email account') : await post<{ ok?: boolean; id?: string }>('/api/email/accounts', body, 'email account');
  return id ?? d.id ?? '';
}
export const deleteEmailAccount = (id: string) => del(`/api/email/accounts/${id}`, 'email account');
export async function testEmailAccount(body: EmailBody): Promise<{ ok: boolean; message: string }> {
  const r = await fetch('/api/email/accounts/test', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(body) });
  const d = (await r.json().catch(() => ({}))) as { ok?: boolean; imap?: { ok?: boolean; error?: string }; smtp?: { ok?: boolean; error?: string; skipped?: boolean }; error?: string; detail?: string; message?: string };
  const parts: string[] = [];
  if (d.imap) parts.push(`IMAP ${d.imap.ok ? 'OK' : d.imap.error ?? 'failed'}`);
  if (d.smtp) parts.push(`SMTP ${d.smtp.skipped ? 'skipped' : d.smtp.ok ? 'OK' : d.smtp.error ?? 'failed'}`);
  const okAll = r.ok && (d.ok ?? (d.imap?.ok !== false && d.smtp?.ok !== false));
  return { ok: !!okAll, message: parts.join(' · ') || d.message || d.error || d.detail || (okAll ? 'OK' : `HTTP ${r.status}`) };
}
export function googleOAuthUrl(accountId: string): string {
  return `/api/email/oauth/google/authorize?account_id=${encodeURIComponent(accountId)}`;
}

/* ── MCP servers ── */

export interface McpServer {
  id: string;
  name: string;
  transport: string;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  url?: string | null;
  is_enabled: boolean;
  status: string;
  tool_count: number;
  enabled_tool_count: number;
  error?: string | null;
  auth_url?: string | null;
  has_oauth?: boolean;
  needs_oauth?: boolean;
  env_mode?: string;
  stderr_log?: string;
}
export interface McpTool {
  name: string;
  description?: string;
  is_disabled?: boolean;
}
export async function listMcpServers(): Promise<McpServer[]> {
  const d = await getJson<McpServer[] | { servers?: McpServer[] }>('/api/mcp/servers');
  return Array.isArray(d) ? d : (d.servers ?? []);
}
export async function addMcpServer(fields: { name: string; transport: string; command?: string; args?: string; env?: string; url?: string; oauth_file?: string; oauth_config?: string }): Promise<{ id?: string; needs_auth?: boolean; auth_url?: string; connected?: boolean; status?: string; tool_count?: number }> {
  return json(`/api/mcp/servers`, { method: 'POST', body: form(fields) }, 'mcp');
}
export const deleteMcpServer = (id: string) => del(`/api/mcp/servers/${id}`, 'mcp');
export const reconnectMcpServer = (id: string) => json<{ connected?: boolean; tool_count?: number; error?: string }>(`/api/mcp/servers/${id}/reconnect`, { method: 'POST' }, 'mcp/reconnect');
export const toggleMcpServer = (id: string, enabled: boolean) => json<unknown>(`/api/mcp/servers/${id}`, { method: 'PATCH', body: form({ is_enabled: String(enabled) }) }, 'mcp/toggle');
export const listMcpTools = (id: string) => getJson<McpTool[]>(`/api/mcp/servers/${id}/tools`);
export const setMcpDisabledTools = (id: string, disabled: string[]) => patch(`/api/mcp/servers/${id}/tools`, { disabled }, 'mcp/tools');
export const mcpOauthExchange = (id: string, callbackUrl: string) => json<unknown>(`/api/mcp/oauth/exchange/${id}`, { method: 'POST', body: form({ callback_url: callbackUrl }) }, 'mcp/oauth');
export const mcpAuthorizeUrl = (id: string) => `/api/mcp/oauth/authorize/${id}`;

/* ── agent tokens (Codex / Claude) ── */

export interface ApiToken {
  id: string;
  name: string;
  scopes: string[];
  token_prefix?: string;
  last_used_at?: string | null;
}
export const AGENT_SCOPES: { key: string; label: string; detail: string }[] = [
  { key: 'todos:read', label: 'Notes', detail: 'Read notes and checklists' },
  { key: 'todos:write', label: 'Notes write', detail: 'Create, update, delete and toggle to-dos' },
  { key: 'documents:read', label: 'Documents', detail: 'Read documents when a document API is enabled' },
  { key: 'documents:write', label: 'Documents write', detail: 'Create and update draft documents' },
  { key: 'email:read', label: 'Mail', detail: 'Read mail when a mail API is enabled' },
  { key: 'email:draft', label: 'Mail drafts', detail: 'Create reply drafts without sending' },
  { key: 'email:send', label: 'Mail send', detail: 'Send mail directly' },
  { key: 'calendar:read', label: 'Calendar', detail: 'Read calendar events' },
  { key: 'calendar:write', label: 'Calendar write', detail: 'Create and update calendar events' },
  { key: 'memory:read', label: 'Memory', detail: 'Read the memory' },
  { key: 'memory:write', label: 'Memory write', detail: 'Write to the memory' },
  { key: 'cookbook:read', label: 'Cookbook', detail: 'List cookbook tasks and tail their output' },
  { key: 'cookbook:launch', label: 'Cookbook launch', detail: 'Launch and stop cookbook serve tasks: runs SSH commands on your servers, bounded by the same allowlist the UI uses' },
  { key: 'agents:dispatch', label: 'Dispatch workers', detail: 'Start local worker jobs from outside the app (POST /api/dispatch, the faustus-workers MCP server) and read their results' },
];
export const AGENT_CONFIGS: Record<'codex' | 'claude', { label: string; word: string; namePrefix: string; defaultName: string; pluginPath: string; buildSetup: (origin: string, token: string) => string }> = {
  codex: {
    label: 'Codex agent',
    word: 'Codex',
    namePrefix: 'codex agent',
    defaultName: 'Codex Agent',
    pluginPath: '/api/codex/plugin.zip',
    buildSetup: (origin, token) => `export ODYSSEUS_URL=${origin}
export ODYSSEUS_API_TOKEN='${token}'
mkdir -p ~/plugins
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/codex/plugin.zip" -o /tmp/odysseus-codex-plugin.zip
python3 -m zipfile -e /tmp/odysseus-codex-plugin.zip ~/plugins
python3 - <<'PY'
import json
from pathlib import Path

p = Path.home() / ".agents" / "plugins" / "marketplace.json"
p.parent.mkdir(parents=True, exist_ok=True)
if p.exists():
    data = json.loads(p.read_text())
else:
    data = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}

data.setdefault("name", "personal")
data.setdefault("interface", {}).setdefault("displayName", "Personal")
plugins = data.setdefault("plugins", [])
entry = {
    "name": "odysseus",
    "source": {"source": "local", "path": "./plugins/odysseus"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}
data["plugins"] = [item for item in plugins if item.get("name") != "odysseus"] + [entry]
p.write_text(json.dumps(data, indent=2) + "\\n")
PY
codex plugin add odysseus@personal
python3 ~/plugins/odysseus/scripts/odysseus_api.py capabilities`,
  },
  claude: {
    label: 'Claude agent',
    word: 'Claude',
    namePrefix: 'claude agent',
    defaultName: 'Claude Agent',
    pluginPath: '/api/claude/plugin.zip',
    buildSetup: (origin, token) => `export ODYSSEUS_URL=${origin}
export ODYSSEUS_API_TOKEN='${token}'
mkdir -p ~/.claude
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/claude/plugin.zip" -o /tmp/odysseus-claude-skill.zip
python3 -m zipfile -e /tmp/odysseus-claude-skill.zip ~/.claude/
python3 ~/.claude/skills/odysseus/scripts/odysseus_api.py capabilities`,
  },
};
export async function listTokens(): Promise<ApiToken[]> {
  const d = await getJson<ApiToken[] | { tokens?: ApiToken[] }>('/api/tokens');
  return Array.isArray(d) ? d : (d.tokens ?? []);
}
/** Which agent a token belongs to, by the same name rule the previous interface used (legacy scoped tokens count as Codex). */
export function agentKindOf(tok: ApiToken): 'codex' | 'claude' | null {
  const n = (tok.name ?? '').toLowerCase();
  if (n.startsWith('claude agent')) return 'claude';
  if (n.startsWith('codex agent')) return 'codex';
  if ((tok.scopes ?? []).some((s) => /^(todos|email|documents):/.test(String(s)))) return 'codex';
  return null;
}
export async function createToken(name: string, scopes: string[]): Promise<{ id: string; token: string }> {
  return json(`/api/tokens`, { method: 'POST', body: form({ name, scopes: scopes.join(',') }) }, 'token');
}
export const updateToken = (id: string, body: { name?: string; scopes?: string[] }) => patch(`/api/tokens/${id}`, body, 'token');
export const deleteToken = (id: string) => del(`/api/tokens/${id}`, 'token');

/* ── vault (Bitwarden) ── */

export interface VaultConfig {
  server_url?: string;
  email?: string;
  bw_installed?: boolean;
  unlocked?: boolean;
  unlocked_at?: string | null;
  logged_in?: boolean;
}
export const vaultConfig = () => getJson<VaultConfig>('/api/vault/config');
export const saveVaultConfig = (body: { server_url: string; email: string }) => post('/api/vault/config', body, 'vault');
export const vaultLogin = (email: string, master_password: string) => post<{ ok?: boolean; already?: boolean; error?: string }>('/api/vault/login', { email, master_password }, 'vault/login');
export const vaultUnlock = (master_password: string) => post<{ ok?: boolean; error?: string }>('/api/vault/unlock', { master_password }, 'vault/unlock');
export const vaultLock = () => json<unknown>('/api/vault/lock', { method: 'POST' }, 'vault/lock');
export const vaultLogout = () => json<unknown>('/api/vault/logout', { method: 'POST' }, 'vault/logout');
