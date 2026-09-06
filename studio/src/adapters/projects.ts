import { ApiError, asArray, getJson } from './api';
import { createSession, listModels, listSessions, type ChatSession, type ModelRoute } from './chat';
import { deleteSession, setSessionFolder } from './sessions';
import { t } from '../i18n';

/**
 * Projects: `/api/projects` and everything under it, in the same shapes
 * projects.js used. A project binds a conversation folder, a working
 * folder, instructions, memory files, objectives and the agent's manners
 * inside that folder.
 */

export interface ContextItem {
  id: string;
  path: string;
  kind: 'folder' | 'file';
  name: string;
}

export interface Project {
  id: string;
  name: string;
  folder?: string | null;
  workspace?: string | null;
  instructions?: string | null;
  enabled?: boolean;
  pinned?: boolean;
  archived?: boolean;
  trusted?: boolean;
  trusted_agents?: boolean;
  review_mode?: boolean;
  checkpoints?: boolean;
  run_tests?: boolean;
  test_command?: string | null;
  review_model?: string | null;
  context_items?: ContextItem[];
  created_at?: number | null;
  updated_at?: number | null;
}

export interface MemoryFile {
  name: string;
  size: number;
  modified: number;
}

export interface ProjectMemory {
  dir: string;
  files: MemoryFile[];
}

export interface Objective {
  id: string;
  title: string;
  status: string;
  priority: number;
  notes: string;
}

export interface ObjectiveEdge {
  from: string;
  to: string;
}

export interface ObjectiveLogEntry {
  ts: string;
  actor: string;
  op: string;
  kind: string;
  id: string;
  why: string;
}

export interface Objectives {
  objectives: Objective[];
  edges: ObjectiveEdge[];
  hints: Record<string, string>;
  log: ObjectiveLogEntry[];
}

export interface AuditEntry {
  ts: number;
  sessionId: string;
  messageId: string;
  request: string;
  files: string[];
  workspace: string;
  tests: string;
  review: string;
  stopReason: string;
  checkpoint: string;
}

export const OBJECTIVE_STATUSES: { value: string; label: string }[] = [
  { value: 'open', label: 'open' },
  { value: 'in_progress', label: 'in progress' },
  { value: 'blocked', label: 'blocked' },
  { value: 'done', label: 'done' },
];

/** The agent's manners inside the folder — same keys, labels and defaults as projects.js. */
export const AGENT_FLAGS: { key: 'trusted' | 'trusted_agents' | 'review_mode' | 'checkpoints' | 'run_tests'; label: string; help: string; def: boolean }[] = [
  { key: 'trusted', label: 'Trusted folder', help: 'File writes inside the folder skip the approval gate (shell and deletions still ask).', def: false },
  { key: 'trusted_agents', label: 'Trusted sub-agents', help: 'Delegating to sub-agents does not ask either.', def: false },
  { key: 'review_mode', label: 'Review mode', help: 'Edits stay pending until you accept them file by file.', def: false },
  { key: 'checkpoints', label: 'Checkpoints', help: 'A snapshot before the first change of every turn, so you can restore without git.', def: true },
  { key: 'run_tests', label: 'Run tests', help: "The project's tests run after every turn that changes files.", def: true },
];

export function flagOn(project: Project, key: (typeof AGENT_FLAGS)[number]['key']): boolean {
  const v = project[key];
  return v == null ? AGENT_FLAGS.find((f) => f.key === key)!.def : Boolean(v);
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (typeof body.error === 'string') detail = body.error;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const jsonInit = (method: string, body?: unknown): RequestInit => ({
  method,
  credentials: 'same-origin',
  headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const base = (id: string) => `/api/projects/${encodeURIComponent(id)}`;

export function listProjects(signal?: AbortSignal): Promise<Project[]> {
  return getJson<unknown>('/api/projects', signal).then((value) => asArray<Project>(value, 'projects'));
}

export function getProject(id: string, signal?: AbortSignal): Promise<Project> {
  return getJson<Project>(base(id), signal);
}

export interface ProjectInput {
  name?: string;
  folder?: string;
  workspace?: string;
  instructions?: string;
  pinned?: boolean;
  archived?: boolean;
  trusted?: boolean;
  trusted_agents?: boolean;
  review_mode?: boolean;
  checkpoints?: boolean;
  run_tests?: boolean;
  test_command?: string;
  review_model?: string;
}

export async function createProject(input: { name: string; folder: string; workspace: string; instructions: string }): Promise<Project> {
  const r = await ok(await fetch('/api/projects', jsonInit('POST', input)), 'projects/create');
  const data = (await r.json()) as { project?: Project } & Project;
  return data.project ?? data;
}

export async function updateProject(id: string, input: ProjectInput): Promise<Project> {
  const r = await ok(await fetch(base(id), jsonInit('PATCH', input)), 'projects/update');
  const data = (await r.json()) as { project?: Project } & Project;
  return data.project ?? data;
}

export async function deleteProject(id: string): Promise<void> {
  await ok(await fetch(base(id), jsonInit('DELETE')), 'projects/delete');
}

/* ── Memory files ── */

export function getMemory(id: string, signal?: AbortSignal): Promise<ProjectMemory> {
  return getJson<ProjectMemory>(`${base(id)}/memory`, signal);
}

export async function readMemoryFile(id: string, name: string): Promise<string> {
  const data = await getJson<{ content?: string }>(`${base(id)}/memory/${encodeURIComponent(name)}`);
  return data.content ?? '';
}

export async function writeMemoryFile(id: string, name: string, content: string): Promise<void> {
  await ok(await fetch(`${base(id)}/memory/${encodeURIComponent(name)}`, jsonInit('PUT', { content })), 'projects/memory');
}

export async function scaffoldMemory(id: string): Promise<void> {
  await ok(await fetch(`${base(id)}/memory/scaffold`, jsonInit('POST')), 'projects/memory/scaffold');
}

/* ── Work roots: the legacy path shape ──
 *
 * `POST {path}` is the shape this endpoint has always taken, and it still
 * creates an editable `work_root` item. Kept as its own function rather
 * than folded into `attachContextSource` because the two mean different
 * things: this one grants the file tools a place to write, and the typed
 * attach below deliberately does not.
 */

export async function addContextRoot(id: string, path: string): Promise<ContextItem> {
  const r = await ok(await fetch(`${base(id)}/context`, jsonInit('POST', { path })), 'projects/context');
  return ((await r.json()) as { item: ContextItem }).item;
}

export async function removeContextRoot(id: string, itemId: string): Promise<void> {
  await ok(await fetch(`${base(id)}/context/${encodeURIComponent(itemId)}`, jsonInit('DELETE')), 'projects/context/delete');
}

/* ── Objectives ── */

const str = (v: unknown) => (typeof v === 'string' ? v : '');

function objectivesFrom(raw: unknown): Objectives {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const list = Array.isArray(r.objectives) ? (r.objectives as Record<string, unknown>[]) : Array.isArray(raw) ? (raw as Record<string, unknown>[]) : [];
  const scores = r.scores && typeof r.scores === 'object' ? (r.scores as Record<string, { hint?: string }>) : {};
  return {
    objectives: list.map((o) => ({
      id: str(o.id),
      title: str(o.title) || str(o.name),
      status: str(o.status) || 'open',
      priority: Math.min(4, Math.max(1, Number(o.priority) || 3)),
      notes: str(o.notes),
    })),
    edges: Array.isArray(r.edges) ? (r.edges as Record<string, unknown>[]).map((e) => ({ from: str(e.from), to: str(e.to) })).filter((e) => e.from && e.to) : [],
    hints: Object.fromEntries(Object.entries(scores).map(([k, v]) => [k, str(v?.hint)]).filter(([, h]) => h)),
    log: Array.isArray(r.log)
      ? (r.log as Record<string, unknown>[]).map((e) => ({
          ts: str(e.ts),
          actor: str(e.actor) || str(e.source),
          op: str(e.op),
          kind: str(e.kind),
          id: str(e.id),
          why: str(e.rationale) || str(e.reason) || str(e.note),
        }))
      : [],
  };
}

export function getObjectives(id: string, signal?: AbortSignal): Promise<Objectives> {
  return getJson<unknown>(`${base(id)}/objectives`, signal).then(objectivesFrom);
}

export async function createObjective(id: string, body: { title: string; priority: number; deps?: string[] }): Promise<void> {
  await ok(await fetch(`${base(id)}/objectives`, jsonInit('POST', body)), 'projects/objectives');
}

export async function patchObjective(id: string, oid: string, body: { status?: string; priority?: number; notes?: string; title?: string }): Promise<void> {
  await ok(await fetch(`${base(id)}/objectives/${encodeURIComponent(oid)}`, jsonInit('PATCH', body)), 'projects/objectives/patch');
}

export async function dropObjective(id: string, oid: string): Promise<void> {
  await ok(await fetch(`${base(id)}/objectives/${encodeURIComponent(oid)}`, jsonInit('DELETE')), 'projects/objectives/drop');
}

/** "OBJ-1, 2 obj-3" → ["OBJ-1", "OBJ-2", "OBJ-3"]; junk is dropped. */
export function parseDeps(text: string): string[] {
  const out = new Set<string>();
  for (const raw of text.split(/[\s,;]+/)) {
    const m = raw.trim().toUpperCase().match(/^(?:OBJ-)?(\d+)$/);
    if (m) out.add(`OBJ-${m[1]}`);
  }
  return [...out];
}

export const objectiveClosed = (o: Objective) => o.status === 'done' || o.status === 'dropped';

/** done/dropped last, then priority (1 first), then numeric id. */
export function sortObjectives(list: Objective[]): Objective[] {
  const num = (id: string) => Number(id.replace(/^OBJ-/i, '')) || 0;
  return list.slice().sort((a, b) => Number(objectiveClosed(a)) - Number(objectiveClosed(b)) || a.priority - b.priority || num(a.id) - num(b.id));
}

/* ── Agent activity ── */

export async function projectAudit(id: string, limit = 100): Promise<AuditEntry[]> {
  const data = await getJson<{ entries?: Record<string, unknown>[] }>(`${base(id)}/audit?limit=${limit}`);
  return (data.entries ?? []).map((e) => ({
    ts: Number(e.ts) || 0,
    sessionId: str(e.session_id),
    messageId: str(e.message_id),
    request: str(e.request),
    files: Array.isArray(e.files) ? e.files.map(String) : [],
    workspace: str(e.workspace),
    tests: str(e.tests),
    review: str(e.review),
    stopReason: str(e.stop_reason),
    checkpoint: str(e.checkpoint),
  }));
}

export async function clearProjectAudit(id: string): Promise<void> {
  await ok(await fetch(`${base(id)}/audit`, jsonInit('DELETE')), 'projects/audit/clear');
}

/* ── Chats ── */

export async function chatsIn(project: Project): Promise<ChatSession[]> {
  if (!project.folder) return [];
  const all = await listSessions();
  return all.filter((s) => s.folder === project.folder).sort((a, b) => Date.parse(b.lastMessageAt ?? b.createdAt ?? '') - Date.parse(a.lastMessageAt ?? a.createdAt ?? ''));
}

/** The chat goes the way every chat goes (projects.js did the same); the
 *  project route for this answers 500 on this server. */
export async function removeChatFromProject(_id: string, sessionId: string): Promise<void> {
  await deleteSession(sessionId);
}

/**
 * A new conversation filed in the project's folder, on the route Studio
 * last used (or the first that can chat). Returns the session id.
 */
export async function startChatInProject(project: Project, route: ModelRoute | null, title?: string): Promise<string> {
  let chosen = route;
  if (!chosen) {
    const routes = await listModels().catch(() => []);
    let lastId = '';
    try {
      lastId = (JSON.parse(localStorage.getItem('faustus_studio_route') ?? '{}') as { id?: string }).id ?? '';
    } catch {
      /* private mode */
    }
    chosen = routes.find((r) => r.id === lastId) ?? routes[0] ?? null;
  }
  if (!chosen) throw new ApiError(t('Choose a model in Studio before starting a project conversation.'), 400);
  const sid = await createSession(title?.trim() ? title.trim().split(/\r?\n/)[0].slice(0, 80) : `New chat · ${project.name}`, chosen);
  if (project.folder) await setSessionFolder(sid, project.folder);
  // An empty PATCH touches updated_at, so "last updated" stays honest.
  await updateProject(project.id, {}).catch(() => null);
  return sid;
}

/* ── Other ── */

/**
 * The exact context block the model receives for this project.
 *
 * This is the endpoint the whole overhaul is arguing for: "el usuario ve qué
 * sabe y qué usará Faustus". It already existed and nothing showed it.
 */
export function getContextPreview(id: string, signal?: AbortSignal): Promise<string> {
  return getJson<{ block?: string }>(`${base(id)}/preview`, signal).then((value) => value.block ?? '');
}

/** Writes an AGENTS.md in the working folder from what the runtime detects. */
export async function draftAgentsMd(workspace: string, language: string): Promise<string> {
  const r = await ok(await fetch('/api/workspace/instructions/draft', jsonInit('POST', { workspace, write: true, language })), 'workspace/instructions');
  const d = (await r.json()) as { written?: string | boolean; exists?: boolean; existing?: string; facts?: { test_command?: string } };
  if (d.written) return d.facts?.test_command ? t('AGENTS.md written (test command detected) — edit it in the folder.') : t('AGENTS.md written (no test runner detected) — edit it in the folder.');
  if (d.exists) return t('The folder already has {file}; nothing written.', { file: (d.existing ?? 'an instructions file').split(/[\\/]/).pop() ?? '' });
  return t('Nothing written.');
}

export function exportProjectUrl(id: string, fmt: string): string {
  return `/api/sessions/export?${new URLSearchParams({ fmt, project: id })}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* ── Context sources: typed links ──
 *
 * A project's context stopped being "a list of paths the agent may edit" and
 * became a list of typed links: what the source is, what it is for, when it
 * may enter a prompt, which revision the project stands on, and — separately
 * from all of that — whether the agent may write to it.
 *
 * The last one is the reason `accessMode` is not a decorative badge.
 * `work_root` widens what the file tools may change; `read_only` is knowledge
 * and widens nothing. A screen that showed them the same way would let
 * somebody grant write access by accident and never find out.
 *
 * These endpoints answer a rejection with 200 and `{ok: false, error: {path,
 * message}}` (the convention of `routes/contracts_routes.py`), so every
 * mutation here goes through `refusalOf` before it believes it worked.
 */

export type LinkKind = 'file' | 'folder' | 'document' | 'artifact' | 'gallery_image';
export type RetrievalPolicy = 'auto' | 'pinned_summary' | 'on_demand' | 'disabled';
export type VersionPolicy = 'latest' | 'pinned' | 'snapshot';
export type AccessMode = 'read_only' | 'work_root';
export type IndexStatus = 'none' | 'queued' | 'indexing' | 'ready' | 'stale' | 'failed';
export type SourceState = 'ok' | 'missing' | 'forbidden' | 'unsupported';

export const LINK_KINDS: LinkKind[] = ['file', 'folder', 'document', 'artifact', 'gallery_image'];
export const RETRIEVAL_POLICIES: RetrievalPolicy[] = ['auto', 'pinned_summary', 'on_demand', 'disabled'];
export const LINK_ROLES: string[] = [
  'requirements', 'reference', 'decision', 'style_reference', 'example',
  'dataset', 'specification', 'output', 'archive',
];

export interface ContextLink {
  id: string;
  kind: LinkKind;
  refId: string;
  path: string;
  label: string;
  role: string;
  tags: string[];
  summary: string;
  retrievalPolicy: RetrievalPolicy;
  versionPolicy: VersionPolicy;
  pinnedVersion: number | null;
  accessMode: AccessMode;
  indexStatus: IndexStatus;
  contentRevision: string;
  enabled: boolean;
  updatedAt: number;
}

export interface Refusal {
  path: string;
  message: string;
}

/** The state of one link as the screen has last seen it. */
export type LinkHealth = 'unchecked' | SourceState;

export interface LinkStatus {
  state: SourceState;
  stale: boolean;
  revision: string;
  effectiveVersion: number;
  message: string;
}

export interface RefreshReport {
  ok: boolean;
  changed: boolean;
  previousRevision: string;
  revision: string;
  indexStatus: IndexStatus;
  state: string;
  message: string;
  link: ContextLink | null;
}

const one = <T extends string>(value: unknown, allowed: readonly T[], fallback: T): T =>
  (allowed as readonly string[]).includes(str(value)) ? (str(value) as T) : fallback;

/** A 200 that says no. Null when the body is an answer rather than a refusal. */
export function refusalOf(raw: unknown): Refusal | null {
  const r = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : null;
  if (!r || r.ok !== false) return null;
  const e = r.error && typeof r.error === 'object' ? (r.error as Record<string, unknown>) : {};
  return { path: str(e.path), message: str(e.message) || 'refused' };
}

export function contextLinkFrom(raw: unknown): ContextLink {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const pinned = Number(r.pinned_version);
  return {
    id: str(r.id),
    kind: one(r.kind, LINK_KINDS, 'file'),
    refId: str(r.ref_id),
    path: str(r.path),
    label: str(r.label) || str(r.name) || str(r.path) || str(r.ref_id),
    role: str(r.role) || 'reference',
    tags: Array.isArray(r.tags) ? r.tags.map(String) : [],
    summary: str(r.summary),
    retrievalPolicy: one(r.retrieval_policy, RETRIEVAL_POLICIES, 'on_demand'),
    versionPolicy: one(r.version_policy, ['latest', 'pinned', 'snapshot'] as const, 'latest'),
    pinnedVersion: Number.isFinite(pinned) && pinned > 0 ? pinned : null,
    accessMode: one(r.access_mode, ['read_only', 'work_root'] as const, 'read_only'),
    indexStatus: one(r.index_status, ['none', 'queued', 'indexing', 'ready', 'stale', 'failed'] as const, 'none'),
    contentRevision: str(r.content_revision),
    enabled: r.enabled !== false,
    updatedAt: Number(r.updated_at) || 0,
  };
}

/** Throw the server's own words for a 200 that refused. */
function orRefusal<T>(body: unknown, build: (b: Record<string, unknown>) => T): T {
  const refusal = refusalOf(body);
  if (refusal) throw new ApiError(refusal.message, 400);
  return build((body && typeof body === 'object' ? body : {}) as Record<string, unknown>);
}

export async function listContextLinks(
  id: string,
  signal?: AbortSignal,
): Promise<{ links: ContextLink[]; revision: number }> {
  const data = await getJson<{ links?: unknown[]; context_revision?: number }>(`${base(id)}/context`, signal);
  return {
    links: (data.links ?? []).map(contextLinkFrom),
    revision: Number(data.context_revision) || 0,
  };
}

export interface AttachInput {
  kind: LinkKind;
  path?: string;
  id?: string;
  label?: string;
  role?: string;
  retrievalPolicy?: RetrievalPolicy;
  accessMode?: AccessMode;
}

export async function attachContextSource(id: string, input: AttachInput): Promise<ContextLink> {
  const r = await ok(
    await fetch(
      `${base(id)}/context`,
      jsonInit('POST', {
        source: { kind: input.kind, path: input.path ?? '', id: input.id ?? '' },
        label: input.label ?? '',
        role: input.role ?? 'reference',
        retrieval_policy: input.retrievalPolicy ?? 'auto',
        access_mode: input.accessMode ?? 'read_only',
      }),
    ),
    'projects/context/attach',
  );
  return orRefusal(await r.json(), (b) => contextLinkFrom(b.link));
}

export interface ContextLinkPatch {
  role?: string;
  retrievalPolicy?: RetrievalPolicy;
  accessMode?: AccessMode;
  label?: string;
  enabled?: boolean;
}

export async function patchContextLink(
  id: string,
  linkId: string,
  patch: ContextLinkPatch,
): Promise<ContextLink> {
  const body: Record<string, unknown> = {};
  if (patch.role !== undefined) body.role = patch.role;
  if (patch.retrievalPolicy !== undefined) body.retrieval_policy = patch.retrievalPolicy;
  if (patch.accessMode !== undefined) body.access_mode = patch.accessMode;
  if (patch.label !== undefined) body.label = patch.label;
  if (patch.enabled !== undefined) body.enabled = patch.enabled;
  const r = await ok(
    await fetch(`${base(id)}/context/${encodeURIComponent(linkId)}`, jsonInit('PATCH', body)),
    'projects/context/patch',
  );
  return orRefusal(await r.json(), (b) => contextLinkFrom(b.link));
}

/** What the source says right now, beside what the link claims. */
export async function inspectContextLink(
  id: string,
  linkId: string,
  signal?: AbortSignal,
): Promise<LinkStatus> {
  const b = await getJson<Record<string, unknown>>(
    `${base(id)}/context/${encodeURIComponent(linkId)}`,
    signal,
  );
  return {
    state: one(b.state, ['ok', 'missing', 'forbidden', 'unsupported'] as const, 'ok'),
    stale: b.stale === true,
    revision: str(b.revision),
    effectiveVersion: Number(b.effective_version) || 0,
    message: str(b.message),
  };
}

export async function refreshContextLink(id: string, linkId: string): Promise<RefreshReport> {
  const r = await ok(
    await fetch(`${base(id)}/context/${encodeURIComponent(linkId)}/refresh`, jsonInit('POST')),
    'projects/context/refresh',
  );
  const b = (await r.json()) as Record<string, unknown>;
  return {
    ok: b.ok === true,
    changed: b.changed === true,
    previousRevision: str(b.previous_revision),
    revision: str(b.revision),
    indexStatus: one(b.index_status, ['none', 'queued', 'indexing', 'ready', 'stale', 'failed'] as const, 'none'),
    state: str(b.state),
    message: str(b.message),
    link: b.link ? contextLinkFrom(b.link) : null,
  };
}

/* ── Pure arithmetic the rows are built from ── */

/** A short, stable revision for a row that has to fit on one line. */
export function shortRevision(revision: string): string {
  const tail = revision.split(':').pop() ?? '';
  return tail.length > 12 ? `${tail.slice(0, 12)}…` : tail || revision;
}

/**
 * Is this link behind the source, as far as the screen can tell?
 *
 * Two different facts, and the row says which. `indexStatus === 'stale'` is
 * the store's own record that the revision moved since the index was built;
 * a `stale` from `inspect` is the resolver saying the source has changed
 * since the link was last refreshed. Either one means "refresh me".
 */
export function linkIsBehind(link: ContextLink, status?: LinkStatus): boolean {
  if (status && status.state === 'ok' && status.stale) return true;
  return link.indexStatus === 'stale';
}

/** Broken: the source is gone, refused, or of a kind nothing can read. */
export function linkIsBroken(status?: LinkStatus): boolean {
  return status !== undefined && status.state !== 'ok';
}

/**
 * Links grouped by role, in the vocabulary's own order, empty roles dropped.
 *
 * The order is `LINK_ROLES`, not alphabetical and not insertion: a project's
 * requirements should be the first thing on the page and its archive the
 * last, and sorting by label would put "Archive of 2019" above them.
 */
export function groupLinksByRole(links: ContextLink[]): { role: string; links: ContextLink[] }[] {
  const known = LINK_ROLES.filter((role) => links.some((l) => l.role === role));
  const unknown = [...new Set(links.map((l) => l.role).filter((r) => !LINK_ROLES.includes(r)))].sort();
  return [...known, ...unknown].map((role) => ({ role, links: links.filter((l) => l.role === role) }));
}

/** "3 sources · 1 editable · 1 needs a refresh", as counts a header can print. */
export function countLinks(
  links: ContextLink[],
  statuses: Record<string, LinkStatus>,
): { total: number; editable: number; behind: number; broken: number; off: number } {
  return {
    total: links.length,
    editable: links.filter((l) => l.accessMode === 'work_root').length,
    behind: links.filter((l) => linkIsBehind(l, statuses[l.id])).length,
    broken: links.filter((l) => linkIsBroken(statuses[l.id])).length,
    off: links.filter((l) => !l.enabled || l.retrievalPolicy === 'disabled').length,
  };
}
