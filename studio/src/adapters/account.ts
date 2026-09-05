import { getJson } from './api';

/**
 * The account and the installation's administration — the previous
 * interface's Account, Users, Tools and the admin cards of System
 * (static/js/settings.js `initAccount`, static/js/admin.js). Same routes.
 */

export interface AuthStatus {
  authenticated?: boolean;
  auth_enabled?: boolean;
  username?: string;
  is_admin?: boolean;
  signup_enabled?: boolean;
  privileges?: Record<string, unknown>;
}

export interface AuthPolicy {
  password_min_length: number;
}

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: string };
    if (d.detail) msg = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export const authStatus = () => getJson<AuthStatus>('/api/auth/status');
export const authPolicy = () => getJson<AuthPolicy>('/api/auth/policy');

export async function changePassword(current: string, next: string): Promise<void> {
  await ok(await fetch('/api/auth/change-password', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ current_password: current, new_password: next }) }), 'change-password');
}

/** Log out and wipe this browser's state, as the previous interface did, so
 *  the next account on this machine inherits nothing (the login form keeps
 *  the last username). */
export async function logout(): Promise<void> {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch {
    /* the cookie is gone anyway */
  }
  try {
    const keep = new Set(['odysseus-last-user']);
    const drop: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k && !keep.has(k)) drop.push(k);
    }
    for (const k of drop) window.localStorage.removeItem(k);
    window.sessionStorage.clear();
  } catch {
    /* private mode */
  }
  window.location.href = '/login';
}

/* ── two-factor ── */

export const tfaStatus = () => getJson<{ enabled: boolean }>('/api/auth/2fa/status');
export async function tfaSetup(): Promise<{ secret: string; qr_code?: string }> {
  const r = await ok(await fetch('/api/auth/2fa/setup', { method: 'POST', credentials: 'same-origin' }), '2fa/setup');
  return (await r.json()) as { secret: string; qr_code?: string };
}
export async function tfaConfirm(code: string): Promise<{ backup_codes: string[] }> {
  const r = await ok(await fetch('/api/auth/2fa/confirm', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ code }) }), '2fa/confirm');
  return (await r.json()) as { backup_codes: string[] };
}
export async function tfaDisable(password: string): Promise<void> {
  await ok(await fetch('/api/auth/2fa/disable', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ password }) }), '2fa/disable');
}

/** Only a data: URL of a raster image is shown as the QR (the server sends one). */
export function safeDataImage(src: unknown): string {
  return typeof src === 'string' && /^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(src) ? src : '';
}

/* ── users (admin) ── */

export interface Privileges {
  can_use_agent?: boolean;
  can_use_browser?: boolean;
  can_use_bash?: boolean;
  can_use_documents?: boolean;
  can_use_research?: boolean;
  can_generate_images?: boolean;
  can_manage_memory?: boolean;
  max_messages_per_day?: number;
  allowed_models?: string[];
  allowed_models_restricted?: boolean;
  block_all_models?: boolean;
}

export interface User {
  username: string;
  is_admin: boolean;
  privileges?: Privileges;
}

export const PRIV_LABELS: { key: keyof Privileges; label: string }[] = [
  { key: 'can_use_agent', label: 'Agent mode' },
  { key: 'can_use_browser', label: 'Browser automation' },
  { key: 'can_use_bash', label: 'Shell / Python / Files' },
  { key: 'can_use_documents', label: 'Document editor' },
  { key: 'can_use_research', label: 'Deep research' },
  { key: 'can_generate_images', label: 'Image generation' },
  { key: 'can_manage_memory', label: 'Memory & skills' },
];

export async function listUsers(): Promise<User[]> {
  const d = await getJson<{ users?: User[] }>('/api/auth/users');
  return d.users ?? [];
}
export async function createUser(username: string, password: string, isAdmin: boolean): Promise<void> {
  await ok(await fetch('/api/auth/users', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ username, password, is_admin: isAdmin }) }), 'users');
}
export async function deleteUser(username: string): Promise<void> {
  await ok(await fetch('/api/auth/users', { method: 'DELETE', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ username }) }), 'users/delete');
}
export async function renameUser(username: string, next: string): Promise<void> {
  await ok(await fetch(`/api/auth/users/${encodeURIComponent(username)}/rename`, { method: 'PUT', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ username: next }) }), 'users/rename');
}
export async function setUserAdmin(username: string, isAdmin: boolean): Promise<void> {
  await ok(await fetch(`/api/auth/users/${encodeURIComponent(username)}/admin`, { method: 'PUT', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ is_admin: isAdmin }) }), 'users/admin');
}
export async function setPrivileges(username: string, patch: Partial<Privileges>): Promise<Privileges> {
  const r = await ok(await fetch(`/api/auth/users/${encodeURIComponent(username)}/privileges`, { method: 'PUT', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(patch) }), 'users/privileges');
  return ((await r.json()) as { privileges?: Privileges }).privileges ?? patch;
}
export async function setOpenSignup(enabled: boolean): Promise<boolean> {
  const r = await ok(await fetch('/api/auth/open-signup', { method: 'PUT', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ enabled }) }), 'open-signup');
  return !!((await r.json()) as { signup_enabled?: boolean }).signup_enabled;
}

/* ── built-in tools (admin) ── */

export interface ToolFlag {
  id: string;
  enabled: boolean;
}

/** Name, blurb and family for each tool id; the previous interface kept the same table (admin.js TOOL_META). */
export const TOOL_META: Record<string, { name: string; desc: string; cat: string }> = {
  bash: { name: 'Shell', desc: 'Execute bash commands', cat: 'Code' },
  python: { name: 'Python', desc: 'Run Python scripts', cat: 'Code' },
  read_file: { name: 'Read file', desc: 'Read files from disk', cat: 'Code' },
  write_file: { name: 'Write file', desc: 'Write or create files', cat: 'Code' },
  web_search: { name: 'Web search', desc: 'Search the web via SearXNG', cat: 'Search' },
  search_chats: { name: 'Search chats', desc: 'Search the conversation history', cat: 'Search' },
  create_document: { name: 'Create document', desc: 'Create new documents', cat: 'Documents' },
  update_document: { name: 'Update document', desc: 'Modify existing documents', cat: 'Documents' },
  edit_document: { name: 'Edit document', desc: 'Find and replace in documents', cat: 'Documents' },
  suggest_document: { name: 'Suggest changes', desc: 'Propose document edits', cat: 'Documents' },
  manage_documents: { name: 'Manage documents', desc: 'List, delete, organise documents', cat: 'Documents' },
  generate_image: { name: 'Generate image', desc: 'Create images with a model', cat: 'Media' },
  manage_memory: { name: 'Memory', desc: 'Save and recall memories', cat: 'Knowledge' },
  manage_skills: { name: 'Skills', desc: 'Learn and use procedures', cat: 'Knowledge' },
  manage_rag: { name: 'RAG / documents', desc: 'Query the indexed documents', cat: 'Knowledge' },
  chat_with_model: { name: 'Chat with a model', desc: 'Talk to another model', cat: 'Multi-agent' },
  pipeline: { name: 'Pipeline', desc: 'Multi-step model workflows', cat: 'Multi-agent' },
  ask_teacher: { name: 'Ask the teacher', desc: 'Query a more capable model', cat: 'Multi-agent' },
  send_to_session: { name: 'Send to a session', desc: 'Send a message to another chat', cat: 'Sessions' },
  create_session: { name: 'Create a session', desc: 'Start a new chat session', cat: 'Sessions' },
  list_sessions: { name: 'List sessions', desc: 'Browse the existing sessions', cat: 'Sessions' },
  manage_session: { name: 'Manage a session', desc: 'Rename, archive, configure', cat: 'Sessions' },
  list_models: { name: 'List models', desc: 'Show the available models', cat: 'System' },
  ui_control: { name: 'UI control', desc: 'Change theme, layout, settings', cat: 'System' },
  manage_tasks: { name: 'Tasks', desc: 'Schedule automated tasks', cat: 'System' },
  api_call: { name: 'API call', desc: 'Make HTTP requests', cat: 'System' },
  manage_endpoints: { name: 'Endpoints', desc: 'Add or remove model endpoints', cat: 'System' },
  manage_mcp: { name: 'MCP servers', desc: 'Manage MCP connections', cat: 'System' },
  manage_webhooks: { name: 'Webhooks', desc: 'Configure webhook events', cat: 'System' },
  manage_tokens: { name: 'API tokens', desc: 'Manage API access tokens', cat: 'System' },
  manage_settings: { name: 'Settings', desc: 'Change app settings', cat: 'System' },
  desktop_screenshot: { name: 'Screenshot', desc: 'Capture the screen (the image goes to the model)', cat: 'Desktop' },
  desktop_list_windows: { name: 'List windows', desc: 'List the visible desktop windows', cat: 'Desktop' },
  desktop_focus_window: { name: 'Focus a window', desc: 'Bring a window to the front', cat: 'Desktop' },
  desktop_click: { name: 'Click', desc: 'Mouse click (asks every time)', cat: 'Desktop' },
  desktop_type: { name: 'Type text', desc: 'Keyboard input (asks every time)', cat: 'Desktop' },
  desktop_key: { name: 'Key combo', desc: 'Shortcuts like ctrl+s (asks every time)', cat: 'Desktop' },
  desktop_scroll: { name: 'Scroll', desc: 'Mouse wheel (asks every time)', cat: 'Desktop' },
};

export async function listTools(): Promise<ToolFlag[]> {
  const d = await getJson<{ tools?: ToolFlag[] }>('/api/tools');
  return d.tools ?? [];
}
/** The route replaces the whole disabled list, so it is always rebuilt from a fresh read. */
export async function setDisabledTools(disabled: string[]): Promise<void> {
  await ok(await fetch('/api/tools', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ disabled }) }), 'tools');
}

/* ── system: logs, backup, wipe (admin) ── */

export async function diagnosticsLogs(limit: number): Promise<string[]> {
  const d = await getJson<{ status?: string; logs?: string[] }>(`/api/diagnostics/logs?limit=${limit}`);
  if (d.status !== 'success' || !Array.isArray(d.logs)) throw new Error('logs');
  return d.logs;
}

export async function exportBackup(): Promise<void> {
  const r = await ok(await fetch('/api/export', { credentials: 'same-origin' }), 'export');
  const blob = await r.blob();
  const m = (r.headers.get('Content-Disposition') ?? '').match(/filename=(.+)/);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1].replace(/"/g, '') : 'faustus_backup.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

export async function importBackup(file: File): Promise<string> {
  const text = (await file.text()).replace(/^\uFEFF/, '').trim();
  const data = JSON.parse(text) as unknown;
  const r = await ok(await fetch('/api/import', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(data) }), 'import');
  const d = (await r.json()) as { message?: string; imported?: Record<string, number> };
  if (d.message) return d.message;
  if (d.imported) return Object.entries(d.imported).map(([k, v]) => `${k}: ${v}`).join(', ');
  return '';
}

export const WIPE_KINDS: { kind: string; label: string; help: string }[] = [
  { kind: 'chats', label: 'Delete all chats', help: 'Every session, message and chat history. Documents, notes and the rest stay.' },
  { kind: 'memory', label: 'Delete all memory', help: 'Clears memory.json, the Memory table and the vector store. Skills are not affected.' },
  { kind: 'skills', label: 'Delete all skills', help: 'Drops data/skills/ (every SKILL.md). Memory is not affected.' },
  { kind: 'notes', label: 'Delete all notes', help: 'Every note, to-do and checklist.' },
  { kind: 'tasks', label: 'Delete all tasks', help: 'Every scheduled task and its run history.' },
  { kind: 'documents', label: 'Delete all documents', help: 'Every document and version: drafts, exports, the library.' },
  { kind: 'gallery', label: 'Delete all images', help: 'Every image record and the upload folder on disk.' },
  { kind: 'calendar', label: 'Delete the calendar', help: 'Every event and every calendar (CalDAV ones too; resync to restore).' },
];

export async function wipe(kind: string): Promise<number> {
  const r = await ok(await fetch(`/api/admin/wipe/${encodeURIComponent(kind)}`, { method: 'DELETE', credentials: 'same-origin' }), `wipe/${kind}`);
  return ((await r.json()) as { count?: number }).count ?? 0;
}
