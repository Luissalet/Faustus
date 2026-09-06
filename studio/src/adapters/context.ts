import { ApiError, asArray, getJson } from './api';
import { parseStamp } from './home';

/**
 * The Context Engine (`/api/context`), shaped for one screen.
 *
 * Two things this layer exists to get right, and both of them are the
 * subsystem's own promises rather than conveniences:
 *
 * **A refusal is an answer.** `routes/context_engine_routes.py` returns a
 * refusal as a 200 with `{"ok": false, "error": {"path", "message"}}`, exactly
 * as the contracts routes do. So the writers here read that body and raise
 * `ContextRefusal` carrying both fields: the screen shows the message the
 * server wrote next to the field it names. "Something went wrong" would throw
 * away the one sentence that tells you which character in your block looked
 * like an API key.
 *
 * **Nothing here decides what is true.** Every derived number — a section's
 * share of the packet, omissions grouped by reason, a verdict's reading — is a
 * pure function below, exported and exercised by `studio/checks/context.check.mjs`.
 * A dashboard whose arithmetic lives inside a component is a dashboard nobody
 * can check.
 */

/* ── the repo's refusal convention ─────────────────────────────────────── */

export interface ContextError {
  /** The field the server named: `block.content`, `feedback.kind`, `<root>`. */
  path: string;
  message: string;
  /** Present on an optimistic-concurrency conflict: the revision it is at now. */
  revision?: number;
}

/** A refusal, raised so a caller cannot mistake it for a success. */
export class ContextRefusal extends Error {
  readonly path: string;
  readonly revision?: number;

  constructor(error: ContextError) {
    super(error.message);
    this.name = 'ContextRefusal';
    this.path = error.path;
    this.revision = error.revision;
  }
}

/* ── small readers: the API is JSON from SQLite, not a typed contract ──── */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const flag = (value: unknown): boolean => value === true || value === 'true' || value === 1;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function countMap(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [key, count] of Object.entries(obj(value))) {
    if (typeof count === 'number' && Number.isFinite(count)) out[key] = count;
  }
  return out;
}

const strList = (value: unknown): string[] => asArray<unknown>(value).map(str).filter(Boolean);

/**
 * A percentage with one decimal, matching `manifest._pct` on the server so the
 * screen and a pasted `render()` report never disagree by a rounding step.
 */
export function pct(part: number, whole: number): number {
  if (!whole) return 0;
  return Math.round(((part * 100) / whole) * 10) / 10;
}

/** Bytes as a person reads them. The store's size is the number that surprises. */
export function formatBytes(bytes: number): string {
  const total = Math.max(0, Math.round(num(bytes)));
  if (total < 1024) return `${total} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = total / 1024;
  let step = 0;
  while (value >= 1024 && step < units.length - 1) {
    value /= 1024;
    step += 1;
  }
  return `${value < 10 ? value.toFixed(1) : String(Math.round(value))} ${units[step]}`;
}

/**
 * The refusal in a 200 body, or null when the call succeeded.
 *
 * `ok: false` with no readable error is still a refusal — saying so beats
 * rendering a success the server did not grant.
 */
export function refusalOf(payload: unknown): ContextError | null {
  const body = obj(payload);
  if (body.ok !== false) return null;
  const error = obj(body.error);
  const message = str(error.message).trim();
  const revision = typeof error.revision === 'number' ? error.revision : undefined;
  return {
    path: str(error.path).trim() || '<root>',
    message: message || 'the server refused this and did not say why',
    revision,
  };
}

/* ── derived views, kept pure so they can be checked ───────────────────── */

/** Which mode the engine is in, from the two settings that decide it. */
export type EngineMode = 'live' | 'shadow' | 'off';

/**
 * `agent_context_engine` and `agent_context_engine_shadow` are independent on
 * the server (shadow is the measurement that earns the other flag), so live
 * wins when both are on: what the model is actually told is the thing a
 * diagnostic screen must not get wrong.
 */
export function engineMode(settings: unknown): EngineMode {
  const values = obj(settings);
  if (flag(values.agent_context_engine)) return 'live';
  if (flag(values.agent_context_engine_shadow)) return 'shadow';
  return 'off';
}

export interface SectionShare {
  kind: string;
  tokens: number;
  /** Share of the packet, not of the window: this answers "what crowded out what". */
  pct: number;
}

/** Sections biggest first, each with its share of the packet's own tokens. */
export function sectionShares(sectionTokens: unknown): SectionShare[] {
  const counts = countMap(sectionTokens);
  let total = 0;
  for (const tokens of Object.values(counts)) total += tokens;
  return Object.entries(counts)
    .map(([kind, tokens]) => ({ kind, tokens, pct: pct(tokens, total) }))
    .sort((a, b) => b.tokens - a.tokens || a.kind.localeCompare(b.kind));
}

export interface OmissionGroup {
  reason: string;
  count: number;
  pct: number;
}

/**
 * Omissions grouped by reason, commonest first.
 *
 * Takes either shape the API can hand over: a list of omission rows (what a
 * compile returns) or the ledger's `omission_counts` map (what survives once
 * the working set has evicted the manifest). One reader, because the question
 * — "why was anything left out?" — is the same either way.
 */
export function omissionsByReason(source: unknown): OmissionGroup[] {
  const counts: Record<string, number> = {};
  if (Array.isArray(source)) {
    for (const row of source) {
      const reason = str(obj(row).reason) || 'unknown';
      counts[reason] = (counts[reason] ?? 0) + 1;
    }
  } else {
    for (const [reason, count] of Object.entries(countMap(source))) {
      if (count > 0) counts[reason] = (counts[reason] ?? 0) + count;
    }
  }
  let total = 0;
  for (const count of Object.values(counts)) total += count;
  return Object.entries(counts)
    .map(([reason, count]) => ({ reason, count, pct: pct(count, total) }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}

export type VerdictTone = 'proved' | 'partial' | 'unproved' | 'contradicted' | 'unknown';

export interface VerdictReading {
  tone: VerdictTone;
  /** The English source string; the screen puts it through `t()`. */
  label: string;
  /**
   * Whether this verdict may be read as a success. Only `proved` may. Colour
   * is never the only signal — the label ships with the tone for exactly this
   * reason — but nothing outside `proved` gets the success treatment either.
   */
  trusted: boolean;
}

const VERDICTS: Record<string, VerdictReading> = {
  proved: { tone: 'proved', label: 'Proved', trusted: true },
  partial: { tone: 'partial', label: 'Partly proved', trusted: false },
  unproved: { tone: 'unproved', label: 'Unproved', trusted: false },
  contradicted: { tone: 'contradicted', label: 'Contradicted', trusted: false },
};

/** One verdict, read. An unknown or missing one is never a success. */
export function verdictReading(verdict: unknown): VerdictReading {
  return (
    VERDICTS[str(verdict).trim().toLowerCase()] ?? {
      tone: 'unknown',
      label: 'No verdict',
      trusted: false,
    }
  );
}

/**
 * How stale the code index is allowed to get before the screen says so:
 * `maintenance.TASK_INTERVALS_S['refresh_code_index']`, which is the interval
 * the server itself considers "worth running again".
 */
export const CODE_INDEX_STALE_S = 1800;

/**
 * Is this index out of date?
 *
 * An empty index is not stale, it is empty, and saying otherwise sends people
 * looking for a problem that is really "nothing has been indexed here yet". An
 * index with rows and no readable timestamp *is* stale: there is no evidence it
 * is current, and a diagnostic must not invent one.
 */
export function codeIndexStale(
  status: { files: number; lastIndexedAt: string },
  now: number = Date.now(),
): boolean {
  if (status.files <= 0) return false;
  const at = parseStamp(status.lastIndexedAt);
  if (!at) return true;
  return now - at > CODE_INDEX_STALE_S * 1000;
}

/* ── diagnostics (§24) ─────────────────────────────────────────────────── */

export interface CacheStats {
  hits: number;
  misses: number;
  evictions: number;
  entries: number;
  bytes: number;
  /** A fraction in [0, 1], as the server rounds it. */
  hitRate: number;
  /** Non-empty when the working set could not be read at all. */
  error: string;
}

export interface SourceReport {
  declared: string[];
  built: string[];
  /** Declared minus built: an adapter whose import failed, named. */
  unavailable: string[];
  error: string;
}

export interface MaintenanceRun {
  name: string;
  ranAt: string;
  ok: boolean;
  changed: number;
  detail: string;
  elapsedMs: number;
}

export interface Diagnostics {
  owner: string;
  packets: number;
  degraded: number;
  degradedPct: number;
  avgTokens: number;
  avgBudgetPct: number;
  receipts: number;
  lastPacketAt: string;
  byIntent: Record<string, number>;
  byConsumer: Record<string, number>;
  omissions: OmissionGroup[];
  storeBytes: number;
  tables: Record<string, number>;
  cache: CacheStats;
  sources: SourceReport;
  lastRun: MaintenanceRun[];
  due: string[];
}

/**
 * The diagnostics body, flattened.
 *
 * Every branch of the server's answer is optional by design: `cache` and
 * `sources` come back as `{"error": …}` when the working set or the registry
 * could not be read, because a diagnostic may never raise. So this reads them
 * defensively and keeps the error text instead of showing a confident zero.
 */
export function normalizeDiagnostics(raw: unknown): Diagnostics {
  const body = obj(raw);
  const compiler = obj(body.compiler);
  const store = obj(body.store);
  const cache = obj(body.cache);
  const sources = obj(body.sources);
  const maintenance = obj(body.maintenance);
  const looks = num(cache.hits) + num(cache.misses);

  const lastRun: MaintenanceRun[] = Object.entries(obj(maintenance.last_run))
    .map(([name, value]) => {
      const row = obj(value);
      return {
        name,
        ranAt: str(row.ran_at),
        ok: row.ok !== false,
        changed: num(row.changed),
        detail: str(row.detail),
        elapsedMs: num(row.elapsed_ms),
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  return {
    owner: str(compiler.owner),
    packets: num(compiler.packets),
    degraded: num(compiler.degraded),
    degradedPct: num(compiler.degraded_pct),
    avgTokens: num(compiler.avg_tokens),
    avgBudgetPct: num(compiler.avg_budget_pct),
    receipts: num(compiler.receipts),
    lastPacketAt: str(compiler.last_packet_at),
    byIntent: countMap(compiler.by_intent),
    byConsumer: countMap(compiler.by_consumer),
    omissions: omissionsByReason(compiler.omissions),
    storeBytes: num(store.bytes) || num(compiler.store_bytes),
    tables: countMap(store.tables),
    cache: {
      hits: num(cache.hits),
      misses: num(cache.misses),
      evictions: num(cache.evictions),
      entries: num(cache.entries),
      bytes: num(cache.bytes),
      hitRate: typeof cache.hit_rate === 'number' ? cache.hit_rate : looks ? num(cache.hits) / looks : 0,
      error: str(cache.error),
    },
    sources: {
      declared: strList(sources.declared),
      built: strList(sources.built),
      unavailable: strList(sources.unavailable),
      error: str(sources.error),
    },
    lastRun,
    due: strList(maintenance.due),
  };
}

/* ── the ledger ────────────────────────────────────────────────────────── */

export interface PacketRow {
  id: string;
  requestId: string;
  createdAt: string;
  sessionId: string;
  projectId: string;
  model: string;
  intent: string;
  phase: string;
  consumer: string;
  tokens: number;
  inputBudget: number;
  /** Share of the input budget this packet spent, or 0 when the window is unknown. */
  budgetPct: number;
  items: number;
  degraded: boolean;
  sectionTokens: Record<string, number>;
  omissionCounts: Record<string, number>;
  /** How many things were left out, all reasons together. */
  omissions: number;
}

export function packetFrom(raw: unknown): PacketRow {
  const row = obj(raw);
  const omissionCounts = countMap(row.omission_counts);
  let omissions = 0;
  for (const count of Object.values(omissionCounts)) omissions += count;
  const tokens = num(row.tokens);
  const inputBudget = num(row.input_budget);
  return {
    id: str(row.packet_id),
    requestId: str(row.request_id),
    createdAt: str(row.created_at),
    sessionId: str(row.session_id),
    projectId: str(row.project_id),
    model: str(row.model),
    intent: str(row.intent),
    phase: str(row.phase),
    consumer: str(row.consumer),
    tokens,
    inputBudget,
    budgetPct: pct(tokens, inputBudget),
    items: num(row.items),
    degraded: row.degraded === true || row.degraded === 1,
    sectionTokens: countMap(row.section_tokens),
    omissionCounts,
    omissions,
  };
}

/**
 * The oldest row in a page of the ledger, and whether the page was full.
 *
 * The ledger is served newest first with a ceiling, so "the oldest packet" is
 * only the oldest *this page can see*. `capped` is what stops the screen from
 * claiming otherwise — a pruning window read off a truncated list is exactly
 * the kind of number that gets believed and is wrong.
 */
export function oldestPacket(packets: PacketRow[], limit: number): { at: string; capped: boolean } {
  let at = '';
  for (const packet of packets) {
    if (packet.createdAt && (!at || packet.createdAt < at)) at = packet.createdAt;
  }
  return { at, capped: limit > 0 && packets.length >= limit };
}

export interface ManifestItem {
  itemId: string;
  section: string;
  sourceType: string;
  sourceRef: string;
  lanes: string[];
  transformation: string;
  generated: boolean;
  trustClass: string;
  authority: string;
  tokens: number;
  chars: number;
  /** Why this is in the packet. §5.2's whole point. */
  reason: string;
}

export interface PacketManifest {
  packetId: string;
  /**
   * False when the working set has evicted the per-item rows. The ledger keeps
   * counts, not rows; an honest miss beats an empty list read as "nothing".
   */
  retained: boolean;
  items: ManifestItem[];
  sections: SectionShare[];
  omissions: OmissionGroup[];
  /**
   * What the compiler wanted a reader to know about how this packet was made.
   * The ledger row has no room for them, so they only exist while the manifest
   * is retained — which is why a degraded packet with none says so rather than
   * showing an empty list.
   */
  warnings: string[];
  degraded: boolean;
  note: string;
}

function manifestItemFrom(raw: unknown): ManifestItem {
  const row = obj(raw);
  return {
    itemId: str(row.context_item_id),
    section: str(row.section) || 'unknown',
    sourceType: str(row.source_type),
    sourceRef: str(row.source_ref),
    lanes: strList(row.retrieval_lanes),
    transformation: str(row.transformation) || 'verbatim',
    generated: row.generated === true,
    trustClass: str(row.trust_class),
    authority: str(row.authority),
    tokens: num(row.tokens),
    chars: num(row.chars),
    reason: str(row.reason),
  };
}

/**
 * The manifest, with its section table derived from the rows themselves.
 *
 * Derived rather than read from `summary.sections` for the same reason the
 * server derives it from the packet: a section total that can disagree with
 * the rows above it is worse than none, because it will be believed.
 */
export function manifestFrom(raw: unknown): PacketManifest {
  const body = obj(raw);
  const retained = body.retained === true;
  const items = asArray<unknown>(body.manifest).map(manifestItemFrom);

  const perSection: Record<string, number> = {};
  for (const item of items) perSection[item.section] = (perSection[item.section] ?? 0) + item.tokens;

  const summary = obj(body.summary);
  return {
    packetId: str(body.packet_id),
    retained,
    items,
    sections: sectionShares(retained ? perSection : body.section_tokens),
    omissions: omissionsByReason(retained ? obj(summary.omissions).by_reason : body.omission_counts),
    warnings: strList(summary.warnings),
    degraded: summary.degraded === true,
    note: str(body.note),
  };
}

/* ── connectable blocks (§7) ───────────────────────────────────────────── */

export const BLOCK_TYPES = [
  'identity',
  'user_profile',
  'project_rules',
  'active_goal',
  'working_state',
  'decision_log',
  'known_failures',
  'tool_policy',
  'style_profile',
  'generation_profile',
  'shared_team_state',
] as const;

export const BLOCK_SCOPES = ['global', 'owner', 'project', 'session', 'agent'] as const;

export interface Block {
  id: string;
  type: string;
  scope: string;
  owner: string;
  projectId: string;
  title: string;
  content: string;
  priority: number;
  maxChars: number;
  alwaysLoaded: boolean;
  trustClass: string;
  sourceRefs: string[];
  revision: number;
  createdAt: string;
  updatedAt: string;
  chars: number;
  /** Longer than its own cap: the reader is being shown a prefix. */
  truncated: boolean;
}

export function blockFrom(raw: unknown): Block {
  const row = obj(raw);
  const content = str(row.content);
  const maxChars = num(row.max_chars);
  return {
    id: str(row.id),
    type: str(row.type),
    scope: str(row.scope),
    owner: str(row.owner),
    projectId: str(row.project_id),
    title: str(row.title),
    content,
    priority: num(row.priority),
    maxChars,
    alwaysLoaded: row.always_loaded === true || row.always_loaded === 1,
    trustClass: str(row.trust_class),
    sourceRefs: strList(row.source_refs),
    revision: num(row.revision),
    createdAt: str(row.created_at),
    updatedAt: str(row.updated_at),
    chars: content.length,
    truncated: maxChars > 0 && content.length > maxChars,
  };
}

export interface RationGroup {
  scope: string;
  owner: string;
  projectId: string;
  blocks: number;
  chars: number;
  maxBlocks: number;
  maxChars: number;
  /** The ids this scope is leaving out. The answer to "why is my block not loading?". */
  demoted: string[];
}

export interface BlockAudit {
  blocks: number;
  alwaysLoaded: number;
  alwaysLoadedGranted: number;
  alwaysLoadedChars: number;
  /** The cap, when a scope has actually hit it; 0 when the server had no reason to say. */
  maxBlocks: number;
  maxChars: number;
  overRation: RationGroup[];
  oversized: { id: string; title: string; chars: number; maxChars: number }[];
  duplicates: { digest: string; ids: string[] }[];
  contradictions: { type: string; scope: string; projectId: string; ids: string[] }[];
  /** Every demoted id, across scopes. */
  demoted: string[];
  ok: boolean;
}

export function auditFrom(raw: unknown): BlockAudit {
  const report = obj(obj(raw).audit);
  const overRation: RationGroup[] = asArray<unknown>(report.over_ration).map((value) => {
    const row = obj(value);
    return {
      scope: str(row.scope),
      owner: str(row.owner),
      projectId: str(row.project_id),
      blocks: num(row.blocks),
      chars: num(row.chars),
      maxBlocks: num(row.max_blocks),
      maxChars: num(row.max_chars),
      demoted: strList(row.demoted),
    };
  });
  const demoted: string[] = [];
  for (const group of overRation) for (const id of group.demoted) if (!demoted.includes(id)) demoted.push(id);

  return {
    blocks: num(report.blocks),
    alwaysLoaded: num(report.always_loaded),
    alwaysLoadedGranted: num(report.always_loaded_granted),
    alwaysLoadedChars: num(report.always_loaded_chars),
    maxBlocks: overRation.reduce((most, group) => Math.max(most, group.maxBlocks), 0),
    maxChars: overRation.reduce((most, group) => Math.max(most, group.maxChars), 0),
    overRation,
    oversized: asArray<unknown>(report.oversized).map((value) => {
      const row = obj(value);
      return {
        id: str(row.id),
        title: str(row.title),
        chars: num(row.chars),
        maxChars: num(row.max_chars),
      };
    }),
    duplicates: asArray<unknown>(report.duplicates).map((value) => ({
      digest: str(obj(value).digest),
      ids: strList(obj(value).ids),
    })),
    contradictions: asArray<unknown>(report.contradictions).map((value) => {
      const row = obj(value);
      return {
        type: str(row.type),
        scope: str(row.scope),
        projectId: str(row.project_id),
        ids: strList(row.ids),
      };
    }),
    demoted,
    ok: report.ok === true,
  };
}

/* ── experiences (§9) and the blackboard (§11) ─────────────────────────── */

export interface Experience {
  id: string;
  projectId: string;
  intent: string;
  problem: string;
  lesson: string;
  result: string;
  verdict: string;
  /** `pattern` or `anti_pattern` — the server's own reading, not ours. */
  role: string;
  stale: boolean;
  score: number;
  helpful: number;
  harmful: number;
  technologies: string[];
  concepts: string[];
  strategy: string[];
  keyDecisions: string[];
  failureModes: string[];
  touchedSymbols: string[];
  verificationRefs: string[];
  createdAt: string;
  updatedAt: string;
}

export function experienceFrom(raw: unknown): Experience {
  const row = obj(raw);
  return {
    id: str(row.id),
    projectId: str(row.project_id),
    intent: str(row.intent),
    problem: str(row.problem),
    lesson: str(row.lesson),
    result: str(row.result),
    verdict: str(row.verdict),
    role: str(row.role) || 'pattern',
    stale: row.stale === true,
    score: num(row.score),
    helpful: num(row.helpful),
    harmful: num(row.harmful),
    technologies: strList(row.technologies),
    concepts: strList(row.concepts),
    strategy: strList(row.strategy),
    keyDecisions: strList(row.key_decisions),
    failureModes: strList(row.failure_modes),
    touchedSymbols: strList(row.touched_symbols),
    verificationRefs: strList(row.verification_refs),
    createdAt: str(row.created_at),
    updatedAt: str(row.updated_at),
  };
}

export interface Finding {
  id: string;
  scope: string;
  projectId: string;
  author: string;
  topic: string;
  kind: string;
  claim: string;
  evidenceRefs: string[];
  tags: string[];
  status: string;
  /** The id of the finding this one corrects, if any. */
  supersedes: string;
  createdAt: string;
  expiresAt: string;
}

export function findingFrom(raw: unknown): Finding {
  const row = obj(raw);
  return {
    id: str(row.id),
    scope: str(row.scope),
    projectId: str(row.project_id),
    author: str(row.author),
    topic: str(row.topic),
    kind: str(row.kind),
    claim: str(row.claim),
    evidenceRefs: strList(row.evidence_refs),
    tags: strList(row.tags),
    status: str(row.status),
    supersedes: str(row.supersedes),
    createdAt: str(row.created_at),
    expiresAt: str(row.expires_at),
  };
}

/**
 * The chain a finding sits on, newest id first.
 *
 * A correction is a new finding pointing at the old one, so the history is a
 * linked list of ids. The board serves current rows only, which means an
 * ancestor is usually not in `byId`: it is still appended — an id you can go
 * and look up is a better answer than a chain that stops silently — and the
 * walk ends there. A cycle ends it too, because a corrupted pointer must not
 * cost the screen its render.
 */
export function supersedeChain(startId: string, byId: Map<string, Finding>): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  let id = startId;
  while (id && !seen.has(id)) {
    seen.add(id);
    chain.push(id);
    id = byId.get(id)?.supersedes ?? '';
  }
  return chain;
}

/* ── the code index (§10) ──────────────────────────────────────────────── */

export interface CodeIndexStatus {
  workspace: string;
  projectId: string;
  files: number;
  symbols: number;
  edges: number;
  byKind: Record<string, number>;
  languages: Record<string, number>;
  lastIndexedAt: string;
}

export function codeIndexStatusFrom(raw: unknown): CodeIndexStatus {
  const row = obj(obj(raw).status);
  return {
    workspace: str(row.workspace),
    projectId: str(row.project_id),
    files: num(row.files),
    symbols: num(row.symbols),
    edges: num(row.edges),
    byKind: countMap(row.by_kind),
    languages: countMap(row.languages),
    lastIndexedAt: str(row.last_indexed_at),
  };
}

export interface CodeSymbol {
  id: string;
  path: string;
  qualname: string;
  kind: string;
  signature: string;
  summary: string;
  startLine: number;
  endLine: number;
  language: string;
  indexedAt: string;
  score: number;
}

export function symbolFrom(raw: unknown): CodeSymbol {
  const row = obj(raw);
  return {
    id: str(row.id),
    path: str(row.path),
    qualname: str(row.qualname),
    kind: str(row.kind),
    signature: str(row.signature),
    summary: str(row.summary),
    startLine: num(row.start_line),
    endLine: num(row.end_line),
    language: str(row.language),
    indexedAt: str(row.indexed_at),
    score: num(row.score),
  };
}

/**
 * `path#Lx-Ly`: enough for a tool — or a person — to open exactly this range.
 * The same pointer `Symbol.source_ref()` writes, minus the `symbol:` prefix
 * that only matters inside a packet.
 */
export function symbolRef(symbol: Pick<CodeSymbol, 'path' | 'startLine' | 'endLine'>): string {
  if (!symbol.path) return '';
  return `${symbol.path}#L${symbol.startLine}-L${symbol.endLine}`;
}

export interface RefreshReport {
  scanned: number;
  reindexed: number;
  removed: number;
  symbols: number;
  edges: number;
  elapsedMs: number;
  /** The walk ran out of file budget before it ran out of tree. */
  truncated: boolean;
}

export function refreshReportFrom(raw: unknown): RefreshReport {
  const row = obj(obj(raw).refresh);
  return {
    scanned: num(row.scanned),
    reindexed: num(row.reindexed),
    removed: num(row.removed),
    symbols: num(row.symbols),
    edges: num(row.edges),
    elapsedMs: num(row.elapsed_ms),
    truncated: row.truncated === true,
  };
}

/** Why a refresh cannot run yet, or `''` when it can. */
export type RefreshBlock = '' | 'unset' | 'unapplied';

/**
 * Is there an applied workspace to refresh, and if not, why not?
 *
 * The Code index tab holds two workspaces: the applied one, which lives in the
 * URL and which every panel on the tab reads, and the one being typed. A
 * refresh walks the applied one. So refreshing a path that was typed and never
 * applied walks nothing, and the server answers — truthfully, and uselessly —
 * "scanned 0, reindexed 0, 0 symbols". That reads as a broken indexer and it is
 * an unconfirmed field.
 *
 * The two empty cases are worth keeping apart, because the sentence a person
 * needs is different: `unapplied` is one keystroke away from working, `unset`
 * needs a path first.
 */
export function refreshBlocker(workspace: unknown, draft: unknown): RefreshBlock {
  if (str(workspace).trim()) return '';
  return str(draft).trim() ? 'unapplied' : 'unset';
}

/* ── talking to the server ─────────────────────────────────────────────── */

const BASE = '/api/context';

function query(params: Record<string, string | number | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    const text = value === undefined ? '' : String(value);
    if (text) parts.push(`${key}=${encodeURIComponent(text)}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

/**
 * A write. Two failures, kept apart, because they need different reactions:
 * a transport or auth failure is an `ApiError` (retry, or log in), and a
 * refusal is a `ContextRefusal` carrying the field and the sentence (fix the
 * input). Collapsing them is how "your block contains an API key" becomes
 * "request failed".
 */
async function send(
  path: string,
  method: 'POST' | 'PATCH' | 'DELETE',
  body?: unknown,
): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers:
      body === undefined
        ? { Accept: 'application/json' }
        : { Accept: 'application/json', 'Content-Type': 'application/json' },
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
  let data: Record<string, unknown> = {};
  try {
    data = obj(await response.json());
  } catch {
    data = {};
  }
  const refusal = refusalOf(data);
  if (refusal) throw new ContextRefusal(refusal);
  return data;
}

export function loadDiagnostics(projectId: string, signal?: AbortSignal): Promise<Diagnostics> {
  return getJson<unknown>(`${BASE}/diagnostics${query({ project_id: projectId })}`, signal).then(
    normalizeDiagnostics,
  );
}

/** The ceiling `MAX_LIMIT` in the routes: asking for more is answered with this. */
export const PACKET_PAGE = 200;

export async function loadPackets(
  projectId: string,
  limit: number = PACKET_PAGE,
  signal?: AbortSignal,
): Promise<PacketRow[]> {
  const data = await getJson<unknown>(`${BASE}/packets${query({ project_id: projectId, limit })}`, signal);
  return asArray<unknown>(obj(data).packets).map(packetFrom);
}

export async function loadManifest(packetId: string, signal?: AbortSignal): Promise<PacketManifest> {
  const data = await getJson<unknown>(`${BASE}/packets/${encodeURIComponent(packetId)}/manifest`, signal);
  return manifestFrom(data);
}

export async function loadBlocks(
  projectId: string,
  type: string,
  scope: string,
  signal?: AbortSignal,
): Promise<Block[]> {
  const data = await getJson<unknown>(
    `${BASE}/blocks${query({ project_id: projectId, type, scope, limit: PACKET_PAGE })}`,
    signal,
  );
  return asArray<unknown>(obj(data).blocks).map(blockFrom);
}

export async function loadBlockAudit(projectId: string, signal?: AbortSignal): Promise<BlockAudit> {
  return auditFrom(await getJson<unknown>(`${BASE}/blocks/audit${query({ project_id: projectId })}`, signal));
}

/** What a person may set on a block; `owner` and `revision` are the server's. */
export interface BlockDraft {
  type: string;
  scope: string;
  project_id: string;
  title: string;
  content: string;
  priority: number;
  max_chars: number;
  always_loaded: boolean;
}

export async function createBlock(draft: BlockDraft): Promise<Block> {
  return blockFrom(obj(await send(`${BASE}/blocks`, 'POST', draft)).block);
}

/**
 * `expected_revision` is optimistic concurrency, not advice: two agents editing
 * the same `working_state` block is the normal case, and last-write-wins loses
 * the first one silently. A conflict comes back as a refusal carrying the
 * revision it is at now, so the screen can say what to do about it.
 */
export async function updateBlock(
  id: string,
  updates: Partial<BlockDraft>,
  expectedRevision: number,
): Promise<Block> {
  const body = await send(`${BASE}/blocks/${encodeURIComponent(id)}`, 'PATCH', {
    updates,
    expected_revision: expectedRevision,
  });
  return blockFrom(obj(body).block);
}

export async function deleteBlock(id: string): Promise<boolean> {
  return obj(await send(`${BASE}/blocks/${encodeURIComponent(id)}`, 'DELETE')).deleted === true;
}

export interface Attachment {
  sessionId: string;
  agentId: string;
  expiresAt: string;
}

function attachmentsOf(body: Record<string, unknown>): Attachment[] {
  return asArray<unknown>(body.attachments).map((value) => {
    const row = obj(value);
    return {
      sessionId: str(row.session_id),
      agentId: str(row.agent_id),
      expiresAt: str(row.expires_at),
    };
  });
}

export async function attachBlock(id: string, to: Partial<Attachment>): Promise<Attachment[]> {
  const body = await send(`${BASE}/blocks/${encodeURIComponent(id)}/attach`, 'POST', {
    session_id: to.sessionId ?? '',
    agent_id: to.agentId ?? '',
    expires_at: to.expiresAt ?? '',
  });
  return attachmentsOf(body);
}

export async function detachBlock(id: string, from: Partial<Attachment>): Promise<Attachment[]> {
  const body = await send(`${BASE}/blocks/${encodeURIComponent(id)}/detach`, 'POST', {
    session_id: from.sessionId ?? '',
    agent_id: from.agentId ?? '',
  });
  return attachmentsOf(body);
}

export async function loadExperiences(
  search: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<Experience[]> {
  const data = await getJson<unknown>(
    `${BASE}/experiences${query({ query: search, project_id: projectId, k: 20 })}`,
    signal,
  );
  return asArray<unknown>(obj(data).experiences).map(experienceFrom);
}

/** `helpful` or `harmful`, with what says so. The route refuses any other kind. */
export async function sendExperienceFeedback(id: string, kind: string, ref: string): Promise<Experience> {
  const body = await send(`${BASE}/experiences/${encodeURIComponent(id)}/feedback`, 'POST', { kind, ref });
  return experienceFrom(obj(body).experience);
}

export async function loadFindings(
  search: string,
  status: string,
  signal?: AbortSignal,
): Promise<Finding[]> {
  const data = await getJson<unknown>(`${BASE}/findings${query({ query: search, status, k: 50 })}`, signal);
  return asArray<unknown>(obj(data).findings).map(findingFrom);
}

export function loadCodeIndexStatus(
  workspace: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<CodeIndexStatus> {
  return getJson<unknown>(
    `${BASE}/code-index/status${query({ workspace, project_id: projectId })}`,
    signal,
  ).then(codeIndexStatusFrom);
}

export async function searchCode(
  search: string,
  workspace: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<CodeSymbol[]> {
  const data = await getJson<unknown>(
    `${BASE}/code-index/search${query({ query: search, workspace, project_id: projectId, k: 25 })}`,
    signal,
  );
  return asArray<unknown>(obj(data).symbols).map(symbolFrom);
}

export async function refreshCodeIndex(
  workspace: string,
  projectId: string,
  full: boolean,
): Promise<RefreshReport> {
  return refreshReportFrom(
    await send(`${BASE}/code-index/refresh`, 'POST', { workspace, project_id: projectId, full }),
  );
}

export interface MaintenanceResult {
  results: MaintenanceRun[];
  due: string[];
}

/**
 * Run the background pass now. `require_human` on the server, and a
 * confirmation in the screen for the same reason: this prunes the ledger,
 * drops index rows, expires findings and vacuums the store. Irreversible, and
 * never urgent.
 */
export async function runMaintenance(workspace: string, projectId: string): Promise<MaintenanceResult> {
  const body = await send(`${BASE}/maintenance/run`, 'POST', {
    workspace,
    project_id: projectId,
  });
  return {
    results: asArray<unknown>(body.results).map((value) => {
      const row = obj(value);
      return {
        name: str(row.name),
        ranAt: '',
        ok: row.ok !== false,
        changed: num(row.changed),
        detail: str(row.detail),
        elapsedMs: num(row.elapsed_ms),
      };
    }),
    due: strList(body.due),
  };
}
