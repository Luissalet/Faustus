import { ApiError, responseReason } from './api';

/**
 * Lote 93 (OBJ-6, contract in scratchpad/CONTRATO_BOARD.md) — the project
 * work board: `/api/projects/{project_id}/board/*`, backend owned by lote
 * 92 (`src/project_board.py`, `routes/board_routes.py`). This adapter only
 * shapes the calls and surfaces what the server said no to — the same
 * split `adapters/git.ts` already draws for source control.
 *
 * Every mutation here is a user's own data (a task list), not an external
 * effect: unlike git's push/pull, nothing requires a human-at-the-keyboard
 * gate server-side, and this adapter does not invent one client-side either.
 */

export type IssueType = 'bug' | 'idea' | 'feature' | 'task' | 'chore';
export type IssueStatus = 'open' | 'in_progress' | 'blocked' | 'done' | 'wontfix' | 'duplicate';
export type IssuePriority = 'P0' | 'P1' | 'P2' | 'P3';
export type IssueLinkKind = 'blocks' | 'blocked_by' | 'relates_to' | 'duplicate_of' | 'discovered_from';
export type IssueRefKind = 'commit' | 'session' | 'file' | 'url';

export const ISSUE_TYPES: IssueType[] = ['bug', 'idea', 'feature', 'task', 'chore'];
export const ISSUE_STATUSES: IssueStatus[] = ['open', 'in_progress', 'blocked', 'done', 'wontfix', 'duplicate'];
export const ISSUE_PRIORITIES: IssuePriority[] = ['P0', 'P1', 'P2', 'P3'];
export const ISSUE_LINK_KINDS: IssueLinkKind[] = ['blocks', 'blocked_by', 'relates_to', 'duplicate_of', 'discovered_from'];

/** English labels; screens wrap these with `t()` — the adapter stays free
 *  of i18n so its pure helpers below can be exercised outside a browser.
 *
 *  `#issue_status` / `#issue_link_kind` tags: plain "Open"/"Blocked"/"Done"/
 *  "Duplicate"/"Blocks" already exist as `t()` keys elsewhere in the app
 *  (a verb -- "Open" a file, "Duplicate" a project -- or a different
 *  gender/sense), with their OWN Spanish translation that must not change.
 *  The `#`-suffix convention (`studio/src/i18n/en.ts`'s own doc comment)
 *  disambiguates: the English UI still reads "Open"/"Blocked"/… either way
 *  (`en.ts` maps the tagged key back to the plain word), while
 *  `docs/ui/i18n/es.tsv` gives the tagged key its own, board-specific
 *  Spanish row alongside the untouched plain one. */
export const TYPE_LABEL: Record<IssueType, string> = {
  bug: 'Bug',
  idea: 'Idea',
  feature: 'Feature',
  task: 'Task',
  chore: 'Chore',
};

export const STATUS_LABEL: Record<IssueStatus, string> = {
  open: 'Open#issue_status',
  in_progress: 'In progress',
  blocked: 'Blocked#issue_status',
  done: 'Done#issue_status',
  wontfix: "Won't fix",
  duplicate: 'Duplicate#issue_status',
};

export const PRIORITY_LABEL: Record<IssuePriority, string> = {
  P0: 'P0 · Urgent',
  P1: 'P1 · High',
  P2: 'P2 · Medium',
  P3: 'P3 · Low',
};

export const LINK_KIND_LABEL: Record<IssueLinkKind, string> = {
  blocks: 'Blocks#issue_link_kind',
  blocked_by: 'Blocked by',
  relates_to: 'Relates to',
  duplicate_of: 'Duplicate of',
  discovered_from: 'Discovered from',
};

/**
 * The state category behind each status (README's vocabulary, same idea as
 * Linear): a workflow-less closed list, not a configurable one.
 */
export type StatusCategory = 'unstarted' | 'started' | 'completed' | 'cancelled';
export const STATUS_CATEGORY: Record<IssueStatus, StatusCategory> = {
  open: 'unstarted',
  in_progress: 'started',
  blocked: 'started',
  done: 'completed',
  wontfix: 'cancelled',
  duplicate: 'cancelled',
};

export interface IssueCompact {
  id: string;
  type: IssueType;
  title: string;
  status: IssueStatus;
  priority: IssuePriority;
  /** Always a string on the wire -- `""` when unassigned, never `null`
   *  (`src.project_board.Store._decode_issue`: `row["assignee"] or ""`).
   *  Callers that want a placeholder for "unassigned" should check
   *  falsiness (`issue.assignee || '—'`), not `?? '—'`. */
  assignee: string;
  labels: string[];
  updated_at: string;
  blocked_by: string[];
}

export interface IssueComment {
  id: string;
  author: string;
  body_md: string;
  created_at: string;
}

export interface IssueEvent {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  actor: string;
  created_at: string;
}

export interface IssueLink {
  id: string;
  kind: IssueLinkKind;
  target_issue_id: string;
}

export interface IssueRef {
  id: string;
  kind: IssueRefKind;
  value: string;
  label: string | null;
  created_at: string;
}

export interface Issue extends IssueCompact {
  body_md: string;
  created_at: string;
  closed_at: string | null;
  created_by: string;
  comments: IssueComment[];
  events: IssueEvent[];
  links: IssueLink[];
  refs: IssueRef[];
}

export interface IssuesListResponse {
  issues: IssueCompact[];
  next_cursor: string | null;
}

export interface BoardSummary {
  key: string;
  counts: Partial<Record<IssueStatus, number>>;
  ready: IssueCompact[];
  in_progress: IssueCompact[];
  recent_done: IssueCompact[];
}

export interface IssueListFilters {
  status?: IssueStatus;
  type?: IssueType;
  assignee?: string;
  q?: string;
  priority?: IssuePriority;
  label?: string;
  limit?: number;
  cursor?: string;
}

export interface CreateIssueInput {
  type: IssueType;
  title: string;
  body_md?: string;
  priority?: IssuePriority;
  assignee?: string | null;
  labels?: string[];
  links?: { kind: IssueLinkKind; target: string }[];
}

export interface UpdateIssueInput {
  title?: string;
  body_md?: string;
  type?: IssueType;
  status?: IssueStatus;
  priority?: IssuePriority;
  assignee?: string | null;
  labels?: string[];
}

export type BoardImportSource = 'objetivos' | 'pendientes' | 'backlog';

export interface ImportPreviewRow {
  id?: string;
  title: string;
  status?: IssueStatus;
  source: BoardImportSource;
  [key: string]: unknown;
}

export interface ImportResult {
  created: number;
  skipped: number;
  preview: ImportPreviewRow[];
}

/**
 * A mutation error's whole JSON body, kept alongside the message a plain
 * `ApiError` would show — same shape as `GitApiError` (`adapters/git.ts`):
 * `error_class` (`board.not_found`, `board.invalid_transition`,
 * `board.claimed`) plus whatever extra the case needs.
 */
export class BoardApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'BoardApiError';
    this.errorClass = errorClass;
    this.payload = payload;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    throw new BoardApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function getBoard<T>(path: string): Promise<T> {
  return request<T>(path);
}

function sendBoard<T>(method: 'POST' | 'PATCH' | 'PUT', path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

function deleteBoard(path: string): Promise<void> {
  return request<void>(path, { method: 'DELETE' });
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const parts = Object.entries(params).filter(([, v]) => v !== undefined && v !== '');
  if (!parts.length) return '';
  return `?${parts.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join('&')}`;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/board`;

export function listIssues(projectId: string, filters: IssueListFilters = {}): Promise<IssuesListResponse> {
  return getBoard(`${base(projectId)}/issues${query(filters as Record<string, string | number | boolean | undefined>)}`);
}

export function listReady(projectId: string): Promise<{ issues: IssueCompact[] }> {
  return getBoard(`${base(projectId)}/ready`);
}

export function getSummary(projectId: string): Promise<BoardSummary> {
  return getBoard(`${base(projectId)}/summary`);
}

export function createIssue(projectId: string, input: CreateIssueInput): Promise<{ issue: Issue }> {
  return sendBoard('POST', `${base(projectId)}/issues`, input);
}

export function getIssue(projectId: string, issueId: string): Promise<{ issue: Issue }> {
  return getBoard(`${base(projectId)}/issues/${encodeURIComponent(issueId)}`);
}

export function updateIssue(projectId: string, issueId: string, patch: UpdateIssueInput): Promise<{ issue: Issue }> {
  return sendBoard('PATCH', `${base(projectId)}/issues/${encodeURIComponent(issueId)}`, patch);
}

export function claimIssue(projectId: string, issueId: string, assignee: string): Promise<{ issue: Issue }> {
  return sendBoard('POST', `${base(projectId)}/issues/${encodeURIComponent(issueId)}/claim`, { assignee });
}

export function addComment(projectId: string, issueId: string, bodyMd: string): Promise<{ comment: IssueComment }> {
  return sendBoard('POST', `${base(projectId)}/issues/${encodeURIComponent(issueId)}/comments`, { body_md: bodyMd });
}

export function addLink(projectId: string, issueId: string, kind: IssueLinkKind, target: string): Promise<{ link: IssueLink }> {
  return sendBoard('POST', `${base(projectId)}/issues/${encodeURIComponent(issueId)}/links`, { kind, target });
}

export function removeLink(projectId: string, issueId: string, linkId: string): Promise<void> {
  return deleteBoard(`${base(projectId)}/issues/${encodeURIComponent(issueId)}/links/${encodeURIComponent(linkId)}`);
}

export function addRef(projectId: string, issueId: string, kind: IssueRefKind, value: string, label?: string): Promise<{ ref: IssueRef }> {
  return sendBoard('POST', `${base(projectId)}/issues/${encodeURIComponent(issueId)}/refs`, { kind, value, label });
}

export function deleteIssue(projectId: string, issueId: string): Promise<void> {
  return deleteBoard(`${base(projectId)}/issues/${encodeURIComponent(issueId)}`);
}

export function importBoard(projectId: string, sources: BoardImportSource[], dryRun: boolean): Promise<ImportResult> {
  return sendBoard('POST', `${base(projectId)}/import`, { sources, dry_run: dryRun });
}

export function setBoardKey(projectId: string, key: string): Promise<{ key: string }> {
  return sendBoard('PUT', `${base(projectId)}/key`, { key });
}

export function exportMdUrl(projectId: string): string {
  return `${base(projectId)}/export.md`;
}

/* ────────────────────────── Pure helpers ──────────────────────────
 * No DOM, no fetch: exercised directly by
 * `studio/checks/l93-board.check.mjs` and reused by the screens. */

export interface BoardColumn {
  id: string;
  statuses: IssueStatus[];
  label: string;
  /** Wontfix/Duplicate start collapsed — CONTRATO_BOARD: "con
   *  Wontfix/Duplicate plegadas". */
  foldedByDefault?: boolean;
}

// Same `#issue_status` tags as `STATUS_LABEL` above -- the kanban's own
// column headers (`t(BOARD_COLUMNS[…].label)`) render the identical words,
// so both must resolve to the identical, board-specific Spanish, not the
// unrelated "Open"/"Blocked"/"Done" already used for other UI.
export const BOARD_COLUMNS: BoardColumn[] = [
  { id: 'open', statuses: ['open'], label: 'Open#issue_status' },
  { id: 'in_progress', statuses: ['in_progress'], label: 'In progress' },
  { id: 'blocked', statuses: ['blocked'], label: 'Blocked#issue_status' },
  { id: 'done', statuses: ['done'], label: 'Done#issue_status' },
  { id: 'closed', statuses: ['wontfix', 'duplicate'], label: "Won't fix / Duplicate", foldedByDefault: true },
];

/** Which column a status belongs to — the inverse of `BoardColumn.statuses`. */
export function columnOf(status: IssueStatus, columns: BoardColumn[] = BOARD_COLUMNS): string {
  return columns.find((c) => c.statuses.includes(status))?.id ?? status;
}

/** The kanban's grouping: every issue lands in exactly one column, in the
 *  order it arrived (callers sort first if they want a particular order
 *  within a column). */
export function groupIssuesByColumn(issues: IssueCompact[], columns: BoardColumn[] = BOARD_COLUMNS): Record<string, IssueCompact[]> {
  const out: Record<string, IssueCompact[]> = {};
  for (const col of columns) out[col.id] = [];
  for (const issue of issues) {
    const col = columnOf(issue.status, columns);
    (out[col] ?? (out[col] = [])).push(issue);
  }
  return out;
}

export interface IssueFilter {
  text?: string;
  type?: IssueType;
  priority?: IssuePriority;
  assignee?: string;
}

/** The board's own client-side filter (title substring + exact type/priority/
 *  assignee) — used for the toolbar's live filtering of an already-loaded
 *  page, distinct from the server-side `q=`/`status=`/… used when (re)loading. */
export function filterIssues(issues: IssueCompact[], filter: IssueFilter): IssueCompact[] {
  const text = (filter.text ?? '').trim().toLowerCase();
  return issues.filter((issue) => {
    if (text && !issue.title.toLowerCase().includes(text) && !issue.id.toLowerCase().includes(text)) return false;
    if (filter.type && issue.type !== filter.type) return false;
    if (filter.priority && issue.priority !== filter.priority) return false;
    if (filter.assignee && (issue.assignee ?? '') !== filter.assignee) return false;
    return true;
  });
}

const PRIORITY_RANK: Record<IssuePriority, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };

/**
 * "ready": priority first, then antiquity — oldest first, `updated_at` as
 * the proxy (the compact shape the contract defines carries no
 * `created_at`; server-side `GET /ready` is authoritative, this comparator
 * is only for re-sorting a client-merged list, e.g. the compact panel's
 * ready+in_progress rows).
 */
export function compareByPriorityThenAge(a: IssueCompact, b: IssueCompact): number {
  const rank = PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority];
  if (rank !== 0) return rank;
  return a.updated_at < b.updated_at ? -1 : a.updated_at > b.updated_at ? 1 : 0;
}

/**
 * `board.invalid_transition` server-side: "solo done→open/in_progress
 * permitido como reopen; wontfix/duplicate son terminales salvo reopen
 * explícito" — done/wontfix/duplicate may only re-open to open/in_progress
 * (or stay put); every other status may move anywhere. Used to grey out
 * invalid drag targets and PATCH options before the server has to say no.
 */
export function allowedNextStatuses(current: IssueStatus): IssueStatus[] {
  if (current === 'done' || current === 'wontfix' || current === 'duplicate') {
    return [current, 'open', 'in_progress'];
  }
  return [...ISSUE_STATUSES];
}

export function canTransition(from: IssueStatus, to: IssueStatus): boolean {
  return from === to || allowedNextStatuses(from).includes(to);
}

/** Escapes everything but the key's own charset (`[A-Z]{3,5}` per the
 *  contract) before it goes into a RegExp — defensive, since a key with a
 *  regex metacharacter should never reach here in practice. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** `FAU-12`-shaped ids for exactly this project's key, global+word-boundary
 *  so it never matches inside a longer token (`AFAU-12`, `FAU-123x`). */
export function issueIdRegex(key: string): RegExp {
  return new RegExp(`\\b${escapeRegExp(key)}-\\d+\\b`, 'g');
}

export type IssueTextSegment = { kind: 'text'; text: string } | { kind: 'issue'; id: string };

/**
 * Splits `text` into plain-text runs and issue-id runs for exactly `key`
 * ("that match the project's key" — CONTRATO_BOARD's chip rule). Pure: the
 * screens (`Transcript.tsx`, `CommitGraph.tsx`) turn `{kind:'issue'}`
 * segments into a clickable chip; this only does the splitting. An empty or
 * falsy `key` never matches anything, so the feature is a no-op until a
 * caller actually has one.
 */
export function linkIssueIds(text: string, key: string): IssueTextSegment[] {
  if (!key || !text) return text ? [{ kind: 'text', text }] : [];
  const re = issueIdRegex(key);
  const out: IssueTextSegment[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    if (match.index > last) out.push({ kind: 'text', text: text.slice(last, match.index) });
    out.push({ kind: 'issue', id: match[0] });
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push({ kind: 'text', text: text.slice(last) });
  return out;
}

/**
 * Lote 93: a UI-only signal, no payload — the board may have changed
 * because of an agent turn (`board_*` tool calls, or the turn simply
 * ending) — mirrors `GIT_REFRESH_EVENT` (`adapters/git.ts`) exactly, one
 * event per concern so a board panel and a git panel refresh
 * independently.
 */
export const BOARD_REFRESH_EVENT = 'faustus:board-refresh';

export function pingBoardRefresh(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(BOARD_REFRESH_EVENT));
}

/** The backend's board-key rule (CONTRATO_BOARD: "3-5 mayúsculas, único"):
 *  checked client-side too so the key editor can disable Save instead of
 *  round-tripping a 400. */
export function isValidBoardKey(key: string): boolean {
  return /^[A-Z]{3,5}$/.test(key);
}
