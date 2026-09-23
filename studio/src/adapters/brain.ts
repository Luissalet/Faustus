import { ApiError, asArray, getJson } from './api';
import type { GraphEdge, GraphNode } from '../lib/graph';

/**
 * The markdown vault (`/api/brain`) — Lot C builds the routes to the exact
 * shapes this module expects (see the contract this lot shipped against);
 * every reader below is defensive about a field being absent or the wrong
 * type, the same discipline `adapters/context.ts` and `adapters/provenance.ts`
 * already apply, because this lot was written and tested before the routes
 * existed to answer it. Nothing here is authoritative: the vault's files are.
 */

const BASE = '/api/brain';

const str = (value: unknown, fallback = ''): string => (typeof value === 'string' ? value : fallback);
const num = (value: unknown, fallback = 0): number => (typeof value === 'number' && Number.isFinite(value) ? value : fallback);
const bool = (value: unknown, fallback = false): boolean => (typeof value === 'boolean' ? value : fallback);
const strOrNull = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const strArray = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []);
export function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function query(params: Record<string, string | number | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : '';
}

async function send(path: string, method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: unknown): Promise<unknown> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const raw = (await response.clone().json()) as { detail?: unknown };
      if (typeof raw.detail === 'string') detail = raw.detail;
    } catch {
      /* not JSON: the status line is all there is */
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/* ── Status & sync ───────────────────────────────────────────────────── */

export interface SyncReport {
  at: string;
  exported: number;
  imported: number;
  created: number;
  suppressed: number;
  conflicts: unknown[];
  guardTripped: boolean;
  errors: string[];
  durationMs: number;
  /** Phase notes (moves, guard, budget) — a list on the wire. */
  notes: string[];
  /** Notes moved to the trash because their source left the store. */
  retired: number;
}

export function syncReportFrom(raw: unknown): SyncReport {
  const r = obj(raw);
  return {
    at: str(r.at),
    exported: num(r.exported),
    imported: num(r.imported),
    created: num(r.created),
    suppressed: num(r.suppressed),
    conflicts: Array.isArray(r.conflicts) ? r.conflicts : [],
    guardTripped: bool(r.guard_tripped),
    errors: strArray(r.errors),
    durationMs: num(r.duration_ms),
    notes: typeof r.notes === 'string' ? (r.notes ? [r.notes] : []) : strArray(r.notes),
    retired: num(r.retired),
  };
}

export interface BrainStatus {
  enabled: boolean;
  vaultDir: string;
  notes: number;
  entities: number;
  relations: number;
  lastSync: SyncReport | null;
  extractionPending: number;
  llmExtractionEnabled: boolean;
  wikiEnabled: boolean;
}

export function statusFrom(raw: unknown): BrainStatus {
  const r = obj(raw);
  const extraction = obj(r.extraction);
  const wiki = obj(r.wiki);
  return {
    enabled: bool(r.enabled, true),
    vaultDir: str(r.vault_dir),
    notes: num(r.notes),
    entities: num(r.entities),
    relations: num(r.relations),
    lastSync: r.last_sync ? syncReportFrom(r.last_sync) : null,
    extractionPending: num(extraction.pending),
    llmExtractionEnabled: bool(extraction.llm_enabled),
    wikiEnabled: bool(wiki.enabled),
  };
}

export function loadStatus(signal?: AbortSignal): Promise<BrainStatus> {
  return getJson<unknown>(`${BASE}/status`, signal).then(statusFrom);
}

export async function runSync(): Promise<SyncReport> {
  return syncReportFrom(await send(`${BASE}/sync`, 'POST'));
}

/* ── Tree ────────────────────────────────────────────────────────────── */

export interface NoteSummary {
  path: string;
  title: string;
  kind: string;
  source: string;
  updatedAt: string;
  size: number;
}

export function noteSummaryFrom(raw: unknown): NoteSummary {
  const r = obj(raw);
  const path = str(r.path);
  return {
    path,
    title: str(r.title) || path.split('/').pop()?.replace(/\.md$/, '') || path,
    kind: str(r.kind, 'note'),
    source: str(r.source),
    updatedAt: str(r.updated_at),
    size: num(r.size),
  };
}

export interface NoteTree {
  folders: string[];
  notes: NoteSummary[];
}

export function noteTreeFrom(raw: unknown): NoteTree {
  const r = obj(raw);
  return { folders: strArray(r.folders), notes: asArray<unknown>(r.notes).map(noteSummaryFrom).filter((n) => n.path) };
}

export function loadTree(signal?: AbortSignal): Promise<NoteTree> {
  return getJson<unknown>(`${BASE}/tree`, signal).then(noteTreeFrom);
}

/* ── One note ────────────────────────────────────────────────────────── */

export interface NoteLink {
  target: string;
  path: string | null;
  resolved: boolean;
  label: string;
}

export interface Backlink {
  path: string;
  title: string;
  context: string;
}

export interface ReadNote {
  path: string;
  title: string;
  kind: string;
  source: string;
  frontmatter: Record<string, unknown>;
  userZone: string;
  generated: string;
  content: string;
  links: NoteLink[];
  backlinks: Backlink[];
  tags: string[];
  updatedAt: string;
  editable: boolean;
}

function noteLinkFrom(raw: unknown): NoteLink {
  const r = obj(raw);
  return { target: str(r.target), path: strOrNull(r.path), resolved: bool(r.resolved), label: str(r.label) || str(r.target) };
}

function backlinkFrom(raw: unknown): Backlink {
  const r = obj(raw);
  return { path: str(r.path), title: str(r.title) || str(r.path), context: str(r.context) };
}

export function readNoteFrom(raw: unknown): ReadNote {
  const r = obj(raw);
  const path = str(r.path);
  return {
    path,
    title: str(r.title) || path.split('/').pop()?.replace(/\.md$/, '') || path,
    kind: str(r.kind, 'note'),
    source: str(r.source),
    frontmatter: obj(r.frontmatter),
    userZone: str(r.user_zone),
    generated: str(r.generated),
    content: str(r.content),
    links: asArray<unknown>(r.links).map(noteLinkFrom),
    backlinks: asArray<unknown>(r.backlinks).map(backlinkFrom),
    tags: strArray(r.tags),
    updatedAt: str(r.updated_at),
    editable: bool(r.editable, true),
  };
}

export function loadNote(path: string, signal?: AbortSignal): Promise<ReadNote> {
  return getJson<unknown>(`${BASE}/note${query({ path })}`, signal).then(readNoteFrom);
}

export interface WriteOutcome {
  note: ReadNote;
  applied: Record<string, unknown>;
}

export async function writeNote(path: string, content: string): Promise<WriteOutcome> {
  const raw = obj(await send(`${BASE}/note`, 'PUT', { path, content }));
  return { note: readNoteFrom(raw.note ?? raw), applied: obj(raw.applied) };
}

export async function createNote(title: string, folder = 'Notes', content = ''): Promise<ReadNote> {
  return readNoteFrom(await send(`${BASE}/note`, 'POST', { title, folder, content }));
}

export interface RenameOutcome {
  note: ReadNote;
  updatedLinks: number;
}

export async function renameNote(path: string, newTitle: string, updateLinks = true): Promise<RenameOutcome> {
  const raw = obj(await send(`${BASE}/note/rename`, 'POST', { path, new_title: newTitle, update_links: updateLinks }));
  return { note: readNoteFrom(raw.note ?? raw), updatedLinks: num(raw.updated_links) };
}

export interface DeleteOutcome {
  trashId: string;
  effect: string;
}

export async function deleteNote(path: string): Promise<DeleteOutcome> {
  const raw = obj(await send(`${BASE}/note${query({ path })}`, 'DELETE'));
  return { trashId: str(raw.trash_id), effect: str(raw.effect) };
}

/* ── Trash ───────────────────────────────────────────────────────────── */

export interface TrashItem {
  id: string;
  path: string;
  title: string;
  source: string;
  effect: string;
  deletedAt: string;
}

function trashItemFrom(raw: unknown): TrashItem {
  const r = obj(raw);
  return { id: str(r.id), path: str(r.path), title: str(r.title) || str(r.path), source: str(r.source), effect: str(r.effect), deletedAt: str(r.deleted_at) };
}

export async function loadTrash(signal?: AbortSignal): Promise<TrashItem[]> {
  const raw = await getJson<unknown>(`${BASE}/trash`, signal);
  return asArray<unknown>(obj(raw).items).map(trashItemFrom);
}

export async function restoreTrash(id: string): Promise<ReadNote> {
  return readNoteFrom(await send(`${BASE}/trash/${encodeURIComponent(id)}/restore`, 'POST'));
}

/* ── Search ──────────────────────────────────────────────────────────── */

export interface SearchHit {
  path: string;
  title: string;
  kind: string;
  snippet: string;
  score: number;
}

function searchHitFrom(raw: unknown): SearchHit {
  const r = obj(raw);
  return { path: str(r.path), title: str(r.title) || str(r.path), kind: str(r.kind, 'note'), snippet: str(r.snippet), score: num(r.score) };
}

export async function searchNotes(q: string, limit = 20, signal?: AbortSignal): Promise<SearchHit[]> {
  if (!q.trim()) return [];
  const raw = await getJson<unknown>(`${BASE}/search${query({ q, limit })}`, signal);
  return asArray<unknown>(obj(raw).results).map(searchHitFrom);
}

/* ── Graph ───────────────────────────────────────────────────────────── */

export type GraphScope = 'notes' | 'entities';

export interface GraphParams {
  center?: string;
  depth?: number;
  kinds?: string[];
  scope?: GraphScope;
}

/** Maps the wire shape onto `lib/graph.ts`'s generic `GraphNode`/`GraphEdge`
 *  (degree and tags travel in `meta`, same convention `provenance.ts` uses). */
function brainNodeFrom(raw: unknown): GraphNode {
  const r = obj(raw);
  const id = str(r.id);
  return { id, kind: str(r.kind, 'note'), label: str(r.label) || id, detail: '', meta: { degree: num(r.degree), tags: strArray(r.tags) } };
}

function brainEdgeFrom(raw: unknown): GraphEdge {
  const r = obj(raw);
  return { from: str(r.from), to: str(r.to), kind: str(r.kind, 'link'), confidence: null, trust: 'declared', why: '', meta: { validNow: r.valid_now === undefined ? null : bool(r.valid_now) } };
}

export interface BrainGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function brainGraphFrom(raw: unknown): BrainGraph {
  const r = obj(raw);
  const nodes = asArray<unknown>(r.nodes).map(brainNodeFrom).filter((n) => n.id);
  const known = new Set(nodes.map((n) => n.id));
  const edges = asArray<unknown>(r.edges).map(brainEdgeFrom).filter((e) => known.has(e.from) && known.has(e.to));
  return { nodes, edges };
}

export function loadGraph(params: GraphParams, signal?: AbortSignal): Promise<BrainGraph> {
  return getJson<unknown>(`${BASE}/graph${query({ center: params.center, depth: params.depth, kinds: params.kinds?.join(','), scope: params.scope })}`, signal).then(brainGraphFrom);
}

/* ── Tags & unresolved links ─────────────────────────────────────────── */

export interface TagCount {
  tag: string;
  count: number;
}

export async function loadTags(signal?: AbortSignal): Promise<TagCount[]> {
  const raw = await getJson<unknown>(`${BASE}/tags`, signal);
  return asArray<unknown>(obj(raw).tags).map((row) => {
    const r = obj(row);
    return { tag: str(r.tag), count: num(r.count) };
  });
}

export interface UnresolvedLink {
  target: string;
  from: string[];
}

export async function loadUnresolved(signal?: AbortSignal): Promise<UnresolvedLink[]> {
  const raw = await getJson<unknown>(`${BASE}/unresolved`, signal);
  return asArray<unknown>(obj(raw).links).map((row) => {
    const r = obj(row);
    return { target: str(r.target), from: strArray(r.from) };
  });
}

/* ── Daily note ──────────────────────────────────────────────────────── */

export function loadDaily(date?: string, signal?: AbortSignal): Promise<ReadNote> {
  return getJson<unknown>(`${BASE}/daily${query({ date })}`, signal).then(readNoteFrom);
}

export function todayIso(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/* ── Entities ────────────────────────────────────────────────────────── */

export interface EntitySummary {
  id: string;
  name: string;
  type: string;
  aliases: string[];
  summary: string;
  hidden: boolean;
  mentions: number;
  relations: number;
  updatedAt: string;
}

export function entitySummaryFrom(raw: unknown): EntitySummary {
  const r = obj(raw);
  return {
    id: str(r.id),
    name: str(r.name),
    type: str(r.type, 'other'),
    aliases: strArray(r.aliases),
    summary: str(r.summary),
    hidden: bool(r.hidden),
    mentions: Array.isArray(r.mentions) ? r.mentions.length : num(r.mentions),
    relations: Array.isArray(r.relations) ? r.relations.length : num(r.relations),
    updatedAt: str(r.updated_at),
  };
}

export async function loadEntities(params: { q?: string; type?: string } = {}, signal?: AbortSignal): Promise<EntitySummary[]> {
  const raw = await getJson<unknown>(`${BASE}/entities${query({ q: params.q, type: params.type })}`, signal);
  return asArray<unknown>(obj(raw).entities).map(entitySummaryFrom);
}

export interface EntityFact {
  sourceRef: string;
  text: string;
  validFrom: string | null;
  validUntil: string | null;
  validNow: boolean;
  createdAt: string;
}

export interface EntityRelation {
  id: string;
  rel: string;
  src: string;
  dst: string;
  srcName: string;
  dstName: string;
  dstValue: string;
  validFrom: string | null;
  validUntil: string | null;
  validAt: boolean;
  confidence: number;
}

export interface TimelineEvent {
  at: string;
  kind: string;
  text: string;
  sourceRef: string;
  itemId: string;
}

function factFrom(raw: unknown): EntityFact {
  const r = obj(raw);
  return { sourceRef: str(r.source_ref), text: str(r.text), validFrom: strOrNull(r.valid_from), validUntil: strOrNull(r.valid_until), validNow: bool(r.valid_now, true), createdAt: str(r.created_at) };
}

function relationFrom(raw: unknown): EntityRelation {
  const r = obj(raw);
  return {
    id: str(r.id),
    rel: str(r.rel),
    src: str(r.src),
    dst: str(r.dst),
    srcName: str(r.src_name) || str(r.src),
    dstName: str(r.dst_name) || str(r.dst) || str(r.dst_value),
    dstValue: str(r.dst_value),
    validFrom: strOrNull(r.valid_from),
    validUntil: strOrNull(r.valid_until),
    validAt: bool(r.valid_at, true),
    confidence: num(r.confidence, 1),
  };
}

function timelineEventFrom(raw: unknown): TimelineEvent {
  const r = obj(raw);
  return { at: str(r.at), kind: str(r.kind), text: str(r.text), sourceRef: str(r.source_ref), itemId: str(r.item_id) };
}

export interface EntityProfile {
  entity: EntitySummary;
  path: string | null;
  facts: EntityFact[];
  relations: EntityRelation[];
  history: EntityRelation[];
  timeline: TimelineEvent[];
  summary: string;
  summarySources: string[];
}

export function entityProfileFrom(raw: unknown): EntityProfile {
  const r = obj(raw);
  return {
    entity: entitySummaryFrom(r.entity),
    path: strOrNull(r.path),
    facts: asArray<unknown>(r.facts).map(factFrom),
    relations: asArray<unknown>(r.relations).map(relationFrom),
    history: asArray<unknown>(r.history).map(relationFrom),
    timeline: asArray<unknown>(r.timeline).map(timelineEventFrom),
    summary: str(r.summary),
    summarySources: strArray(r.summary_sources),
  };
}

export function loadEntityProfile(id: string, asOf?: string, signal?: AbortSignal): Promise<EntityProfile> {
  return getJson<unknown>(`${BASE}/entities/${encodeURIComponent(id)}${query({ as_of: asOf })}`, signal).then(entityProfileFrom);
}

export interface EntityPatch {
  name?: string;
  type?: string;
  aliases?: string[];
  summary?: string;
  hidden?: boolean;
}

export async function updateEntity(id: string, patch: EntityPatch): Promise<EntitySummary> {
  return entitySummaryFrom(await send(`${BASE}/entities/${encodeURIComponent(id)}`, 'PATCH', patch));
}

export async function mergeEntities(keep: string, merge: string): Promise<EntitySummary> {
  return entitySummaryFrom(await send(`${BASE}/entities/merge`, 'POST', { keep, merge }));
}

export async function loadTimeline(params: { entity?: string; q?: string; limit?: number } = {}, signal?: AbortSignal): Promise<TimelineEvent[]> {
  const raw = await getJson<unknown>(`${BASE}/timeline${query({ entity: params.entity, q: params.q, limit: params.limit })}`, signal);
  return asArray<unknown>(obj(raw).events).map(timelineEventFrom);
}

/* ── Background passes ───────────────────────────────────────────────── */

export interface ExtractReport {
  processed: number;
  entities: number;
  relations: number;
  errors: string[];
}

export function extractReportFrom(raw: unknown): ExtractReport {
  const r = obj(raw);
  return { processed: num(r.processed), entities: num(r.entities), relations: num(r.relations), errors: strArray(r.errors) };
}

export async function runExtract(limit?: number): Promise<ExtractReport> {
  return extractReportFrom(await send(`${BASE}/extract`, 'POST', limit ? { limit } : {}));
}

export interface WikiRefreshReport {
  refreshed: number;
  skipped: number;
  errors: string[];
}

export function wikiRefreshReportFrom(raw: unknown): WikiRefreshReport {
  const r = obj(raw);
  return { refreshed: num(r.refreshed), skipped: num(r.skipped), errors: strArray(r.errors) };
}

export async function runWikiRefresh(entityId?: string): Promise<WikiRefreshReport> {
  return wikiRefreshReportFrom(await send(`${BASE}/wiki/refresh`, 'POST', entityId ? { entity_id: entityId } : {}));
}

/* ── Settings ────────────────────────────────────────────────────────── */

export interface BrainSettings {
  memory_temporal_parse: boolean;
  memory_temporal_supersede: boolean;
  brain_enabled: boolean;
  brain_vault_dir: string;
  brain_vault_sync_seconds: number;
  brain_entity_extraction: boolean;
  brain_llm_extraction: boolean;
  brain_wiki_summaries: boolean;
  brain_context_source: boolean;
  owner_display_name: string;
}

export function brainSettingsFrom(raw: unknown): BrainSettings {
  const r = obj(raw);
  return {
    memory_temporal_parse: bool(r.memory_temporal_parse, true),
    memory_temporal_supersede: bool(r.memory_temporal_supersede, true),
    brain_enabled: bool(r.brain_enabled, true),
    brain_vault_dir: str(r.brain_vault_dir),
    brain_vault_sync_seconds: num(r.brain_vault_sync_seconds, 60),
    brain_entity_extraction: bool(r.brain_entity_extraction, true),
    brain_llm_extraction: bool(r.brain_llm_extraction),
    brain_wiki_summaries: bool(r.brain_wiki_summaries),
    brain_context_source: bool(r.brain_context_source, true),
    owner_display_name: str(r.owner_display_name),
  };
}

export function loadBrainSettings(signal?: AbortSignal): Promise<BrainSettings> {
  return getJson<unknown>(`${BASE}/settings`, signal).then(brainSettingsFrom);
}

export async function saveBrainSettings(patch: Partial<BrainSettings>): Promise<BrainSettings> {
  return brainSettingsFrom(await send(`${BASE}/settings`, 'PUT', patch));
}

/* ── Composing a note's file text client-side ───────────────────────────
 * `notes.write_note` takes the WHOLE new file text, not a diff. The editor
 * only ever changes the user zone or a handful of frontmatter fields, so
 * this reassembles the rest byte-for-byte from what `read_note` already
 * gave us — the generated zone is never touched here, only re-sent as-is,
 * because only a sync (server-side `render.py`) is allowed to rewrite it. */

const MARKER = '%% faustus:generated — edits below this line are replaced on the next sync %%';

/** A string written so any YAML reader gives the SAME string back: quoted
 *  whenever it could read as something else (a date, `10:30`, `1_000`,
 *  `yes`, a number) or holds YAML syntax. */
function yamlScalar(value: string): string {
  if (value === '') return '""';
  if (/^(true|false|null|~|yes|no|on|off|y|n)$/i.test(value)) return JSON.stringify(value);
  if (/^[-+.]?\d/.test(value)) return JSON.stringify(value);
  if (/^[A-Za-z_][\w ./@+-]*$/.test(value) && !/\s$/.test(value)) return value;
  return JSON.stringify(value);
}

function yamlValue(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'boolean' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) {
    return `[${value.map((v) => (v !== null && typeof v === 'object' ? JSON.stringify(v) : yamlScalar(String(v)))).join(', ')}]`;
  }
  // A nested mapping as flow-style JSON, which is valid YAML — never "[object Object]".
  if (typeof value === 'object') return JSON.stringify(value);
  return yamlScalar(String(value));
}

/** A flat YAML block good enough for the frontmatter shapes this vault
 *  uses — scalars and single-level lists, in the order the caller gives
 *  them (a stable frontmatter key order matters for a clean diff). */
export function frontmatterToYaml(frontmatter: Record<string, unknown>): string {
  const lines: string[] = [];
  for (const [key, value] of Object.entries(frontmatter)) {
    if (value === undefined || value === null || value === '') continue;
    lines.push(`${key}: ${yamlValue(value)}`);
  }
  return lines.join('\n');
}

const TOP_KEY = /^([^\s#:'"-][^:]*?)\s*:(?:\s|$)/;

/** Applies `patch` to a frontmatter YAML block AS TEXT: each patched key's
 *  own line(s) are replaced (or appended), every other line — comments,
 *  quoting, key order, values this editor does not understand — stays
 *  byte-for-byte what the file had. A null value is written as `null`, so
 *  clearing a field is an explicit change the server can see. */
export function patchFrontmatterYaml(yaml: string, patch: Record<string, unknown>): string {
  const lines = yaml === '' ? [] : yaml.split(/\r?\n/);
  for (const [key, value] of Object.entries(patch)) {
    const entry = `${key}: ${yamlValue(value)}`;
    let start = -1;
    for (let i = 0; i < lines.length; i += 1) {
      const m = TOP_KEY.exec(lines[i]);
      if (m && m[1].trim() === key) {
        start = i;
        break;
      }
    }
    if (start < 0) {
      lines.push(entry);
      continue;
    }
    let end = start + 1;
    while (end < lines.length && (/^\s+\S/.test(lines[end]) || /^-(\s|$)/.test(lines[end]))) end += 1;
    lines.splice(start, end - start, entry);
  }
  return lines.join('\n');
}

/** The raw YAML text between the opening and closing `---` of a note, or
 *  null when the file does not open with a frontmatter block. */
export function rawFrontmatter(content: string): string | null {
  const text = content.replace(/^﻿/, '');
  if (/^---\r?\n---[ \t]*(?:\r?\n|$)/.test(text)) return '';
  const m = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(text);
  return m ? m[1] : null;
}

/** Rebuilds the note's full file text from the (possibly edited) user zone
 *  and a PATCH of the frontmatter keys the person changed — only those keys
 *  are rewritten; the rest of the frontmatter block goes back exactly as
 *  the file had it (from `note.content`). The generated zone — and whether
 *  there even is one — stays exactly as `read_note` reported it. */
export function composeNoteContent(note: ReadNote, userZone: string, patch: Record<string, unknown> = {}): string {
  const hasFrontmatter = Object.keys(note.frontmatter ?? {}).length > 0;
  const raw = hasFrontmatter && note.content ? rawFrontmatter(note.content) : null;
  const base = raw !== null ? raw : frontmatterToYaml(note.frontmatter ?? {});
  const yaml = Object.keys(patch).length ? patchFrontmatterYaml(base, patch) : base;
  const head = yaml ? `---\n${yaml}\n---\n\n` : '';
  const body = userZone.replace(/\s+$/, '');
  if (!note.source && !note.generated.trim()) return `${head}${body}\n`;
  return `${head}${body}\n\n${MARKER}\n\n${note.generated}\n`;
}
