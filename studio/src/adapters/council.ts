import { asArray, getJson } from './api';

/**
 * Council (`/api/council`), shaped for one screen.
 *
 * A council room is not "several chatbots answering at once". The rule the
 * whole subsystem exists to hold is that many models may think, object and
 * review the same matter, and only the designated owner executes each effect
 * or modifies each resource — so the two things this layer must never lose
 * are WHO said something and WHAT they are allowed to do about it.
 *
 * Three consequences, and they are the reason this file exists at all:
 *
 * **Nothing is inferred from text.** A message's author is the `author_id`
 * and `author_kind` the server stored. A model that opens its answer with
 * `Usuario:` does not change hands; `claims_identity` is a warning drawn next
 * to the message, never a fact this layer acts on.
 *
 * **The vocabularies come from `/api/council/config`.** Policies, roles, tool
 * profiles, ceilings and default budgets are read from the server rather than
 * restated here, so a role added to `contracts.ROLES` reaches the form
 * without a second edit in the front end.
 *
 * **Every derived claim is a pure function below**, exported and driven by
 * `studio/checks/council.check.mjs`: which turn a message belongs to, whether
 * a room is blocked, where a reconnection resumes, what a verdict is called
 * and who is holding a resource. A panel whose arithmetic lives inside a
 * component is a panel nobody can check.
 */

/* ── small readers: the API is JSON out of SQLite, not a typed contract ── */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const flag = (value: unknown): boolean => value === true;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

const strList = (value: unknown): string[] => asArray<unknown>(value).map(str).filter(Boolean);

function countMap(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [key, count] of Object.entries(obj(value))) {
    if (typeof count === 'number' && Number.isFinite(count)) out[key] = count;
  }
  return out;
}

/* ── the refusal convention ─────────────────────────────────────────────── */

/**
 * A command answers with a stable token and a sentence: the token is what a
 * caller branches on, the sentence is for the person reading. Reading the
 * sentence to decide what happened is exactly the habit this repository
 * already paid for once.
 */
export interface CouncilError {
  code: string;
  detail: string;
  /** Present on `revision_conflict`: the revision the room is actually at. */
  revision?: number;
  /** Present on `unknown_command`: what it could have been. */
  validCommands?: string[];
}

export class CouncilRefusal extends Error {
  readonly code: string;
  readonly revision?: number;
  readonly validCommands?: string[];

  constructor(error: CouncilError) {
    super(error.detail);
    this.name = 'CouncilRefusal';
    this.code = error.code;
    this.revision = error.revision;
    this.validCommands = error.validCommands;
  }
}

/**
 * The refusal inside a 200 body, or null when the call succeeded.
 *
 * Two shapes reach here and both are the repository's own: the service answers
 * `{ok: false, error: "<token>", detail}` and the route re-shapes it into the
 * `{ok: false, error: {path, message, code}}` that `contracts_routes` uses.
 * Reading both is one branch; guessing which one arrived from the prose would
 * be the habit this file exists to avoid.
 */
export function refusalOf(payload: unknown): CouncilError | null {
  const body = obj(payload);
  if (body.ok !== false) return null;
  const nested = obj(body.error);
  const code = str(body.error).trim() || str(nested.code).trim();
  const detail = str(body.detail).trim() || str(nested.message).trim();
  const revision = typeof body.revision === 'number' ? body.revision : undefined;
  const valid = strList(body.valid_commands);
  return {
    code: code || 'command_failed',
    detail: detail || 'the room refused this and did not say why',
    revision,
    validCommands: valid.length ? valid : undefined,
  };
}

/* ── the shapes, as the server sends them ───────────────────────────────── */

export interface Budgets {
  maxRounds: number;
  maxTurns: number;
  maxWallSeconds: number;
  maxTotalTokens: number;
  maxParallel: number;
}

export interface RoleOption {
  id: string;
  /** The profile the role justifies before any room narrows it. */
  defaultProfile: string;
}

export interface CouncilConfig {
  policies: string[];
  roles: RoleOption[];
  toolProfiles: string[];
  /**
   * The profiles that may modify something, from the server's own
   * `contracts.WRITING_PROFILES`. `FALLBACK_WRITING_PROFILES` stays for a
   * server older than the field, and is only that: a fallback. Deciding here
   * which profiles write would be a second opinion about permissions, and the
   * one that goes stale.
   */
  writingProfiles: string[];
  policyCeilings: Record<string, string>;
  completionModes: string[];
  sessionStatuses: string[];
  defaultBudgets: Budgets;
  commands: string[];
  errors: string[];
  verdicts: string[];
  orchestratorAvailable: boolean;
}

export interface CouncilRoom {
  id: string;
  owner: string;
  title: string;
  policy: string;
  status: string;
  phase: string;
  workspace: string;
  projectId: string;
  participantIds: string[];
  budgets: Budgets;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface Participant {
  id: string;
  displayName: string;
  kind: string;
  model: string;
  endpointId: string;
  roles: string[];
  toolProfile: string;
  status: string;
  capabilities: string[];
}

export interface CouncilMessage {
  id: string;
  sessionId: string;
  turnId: string;
  authorId: string;
  authorKind: string;
  audience: string[];
  type: string;
  content: string;
  replyTo: string;
  taskId: string;
  decisionId: string;
  visibility: string;
  createdAt: string;
  /**
   * The speaker label the text CLAIMS, when the server sent one. Advisory:
   * the author above is unchanged whatever this says.
   */
  claimsIdentity: string;
}

export interface Task {
  id: string;
  title: string;
  instruction: string;
  owner: string;
  reviewer: string;
  status: string;
  dependsOn: string[];
  resources: string[];
  acceptance: string[];
  runId: string;
  proofId: string;
}

export interface Claim {
  id: string;
  kind: string;
  resource: string;
  holderId: string;
  state: string;
  taskId: string;
  note: string;
  expiresAt: string;
}

export interface Objection {
  id: string;
  targetKind: string;
  targetId: string;
  authorId: string;
  severity: string;
  claim: string;
  proposedResolution: string;
  status: string;
  evidence: string[];
  createdAt: string;
}

export interface Decision {
  id: string;
  question: string;
  status: string;
  chosen: string;
  alternatives: string[];
  rationale: string[];
  supporters: string[];
  dissenters: string[];
  evidence: string[];
  supersedes: string;
  createdAt: string;
}

export interface Ledger {
  sessionId: string;
  tasks: Task[];
  claims: Claim[];
  heldClaims: Claim[];
  objections: Objection[];
  openObjections: Objection[];
  decisions: Decision[];
  readyTaskIds: string[];
  blockedTaskIds: string[];
  blockingOpen: boolean;
  counts: Record<string, number>;
  unreadable: boolean;
}

export interface Usage {
  sessionId: string;
  budgets: Budgets;
  /** Spend so far. Zero after a restart: the counters live in the process. */
  spent: { rounds: number; turns: number; wallSeconds: number; totalTokens: number; parallel: number };
  exhausted: string;
  stopReason: string;
  turns: number;
  calls: number;
  waitedMs: number;
}

export interface CouncilEvent {
  id: string;
  seq: number;
  name: string;
  sessionId: string;
  activityId: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

/* ── parsers: total, so one bad row never blanks a room ─────────────────── */

/** The three names the front falls back to when `/config` omits them. */
export const FALLBACK_WRITING_PROFILES = ['scoped_write', 'integrator', 'full_with_gates'];

export function budgetsFrom(raw: unknown): Budgets {
  const b = obj(raw);
  return {
    maxRounds: num(b.max_rounds),
    maxTurns: num(b.max_turns),
    maxWallSeconds: num(b.max_wall_seconds),
    maxTotalTokens: num(b.max_total_tokens),
    maxParallel: num(b.max_parallel),
  };
}

export function budgetsPayload(b: Budgets): Record<string, number> {
  return {
    max_rounds: b.maxRounds,
    max_turns: b.maxTurns,
    max_wall_seconds: b.maxWallSeconds,
    max_total_tokens: b.maxTotalTokens,
    max_parallel: b.maxParallel,
  };
}

export function configFrom(raw: unknown): CouncilConfig {
  const c = obj(raw);
  const writing = strList(c.writing_profiles);
  return {
    policies: strList(c.policies),
    roles: asArray<unknown>(c.roles).map((row) => {
      const r = obj(row);
      return { id: str(r.id), defaultProfile: str(r.default_profile) || 'none' };
    }).filter((r) => r.id),
    toolProfiles: strList(c.tool_profiles),
    writingProfiles: writing.length ? writing : [...FALLBACK_WRITING_PROFILES],
    policyCeilings: Object.fromEntries(
      Object.entries(obj(c.policy_tool_ceilings)).map(([k, v]) => [k, str(v) || 'none']),
    ),
    completionModes: strList(c.completion_modes),
    sessionStatuses: strList(c.session_statuses),
    defaultBudgets: budgetsFrom(c.default_budgets),
    commands: strList(c.commands),
    errors: strList(c.errors),
    verdicts: strList(c.verdicts),
    orchestratorAvailable: flag(c.orchestrator_available),
  };
}

export function roomFrom(raw: unknown): CouncilRoom {
  const s = obj(raw);
  return {
    id: str(s.id),
    owner: str(s.owner),
    title: str(s.title),
    policy: str(s.policy) || 'chat',
    status: str(s.status) || 'draft',
    phase: str(s.phase),
    workspace: str(s.workspace),
    projectId: str(s.project_id),
    participantIds: strList(s.participants),
    budgets: budgetsFrom(s.budgets),
    createdAt: str(s.created_at),
    updatedAt: str(s.updated_at),
    revision: num(s.revision) || 1,
  };
}

export function participantFrom(raw: unknown): Participant {
  const p = obj(raw);
  const id = str(p.id);
  return {
    id,
    displayName: str(p.display_name) || id,
    kind: str(p.kind) || 'model',
    model: str(p.model),
    endpointId: str(p.endpoint_id),
    roles: strList(p.roles),
    toolProfile: str(p.tool_profile) || 'none',
    status: str(p.status),
    capabilities: strList(p.capabilities),
  };
}

export function messageFrom(raw: unknown): CouncilMessage {
  const m = obj(raw);
  const meta = obj(m.metadata);
  return {
    id: str(m.id),
    sessionId: str(m.session_id),
    turnId: str(m.turn_id),
    authorId: str(m.author_id),
    authorKind: str(m.author_kind) || 'model',
    audience: strList(m.audience),
    type: str(m.message_type) || 'message',
    content: str(m.content),
    replyTo: str(m.reply_to),
    taskId: str(m.task_id),
    decisionId: str(m.decision_id),
    visibility: str(m.visibility) || 'room',
    createdAt: str(m.created_at),
    // §3.2's warning. The transcript route computes it per row; `metadata` is
    // the older spelling and stays as a fallback so a message stored by a
    // previous build still shows the warning it was stored with. Reading only
    // metadata — which is what this line used to do — meant the field was
    // always empty, because nothing on the server ever wrote it there.
    claimsIdentity: str(m.claims_identity) || str(meta.claims_identity),
  };
}

export function taskFrom(raw: unknown): Task {
  const t = obj(raw);
  return {
    id: str(t.id),
    title: str(t.title),
    instruction: str(t.instruction),
    owner: str(t.owner_participant_id),
    reviewer: str(t.reviewer_participant_id),
    status: str(t.status) || 'pending',
    dependsOn: strList(t.depends_on),
    resources: strList(t.claimed_resources),
    acceptance: strList(t.acceptance),
    runId: str(t.run_id),
    proofId: str(t.proof_id),
  };
}

export function claimFrom(raw: unknown): Claim {
  const c = obj(raw);
  return {
    id: str(c.id),
    kind: str(c.kind) || 'file',
    resource: str(c.resource),
    holderId: str(c.holder_id),
    state: str(c.state) || 'requested',
    taskId: str(c.task_id),
    note: str(c.note),
    expiresAt: str(c.expires_at),
  };
}

export function objectionFrom(raw: unknown): Objection {
  const o = obj(raw);
  return {
    id: str(o.id),
    // `target_kind` is the contract's name; `target` is the plan's, and the
    // close renders the second. Reading both keeps one shape on this side.
    targetKind: str(o.target_kind) || str(o.target) || 'message',
    targetId: str(o.target_id),
    authorId: str(o.author_id),
    severity: str(o.severity) || 'concern',
    claim: str(o.claim),
    proposedResolution: str(o.proposed_resolution),
    status: str(o.status) || 'open',
    evidence: strList(o.evidence_refs).length ? strList(o.evidence_refs) : strList(o.evidence),
    createdAt: str(o.created_at),
  };
}

export function decisionFrom(raw: unknown): Decision {
  const d = obj(raw);
  return {
    id: str(d.id),
    question: str(d.question),
    status: str(d.status) || 'proposed',
    chosen: str(d.chosen),
    alternatives: strList(d.alternatives),
    rationale: strList(d.rationale),
    supporters: strList(d.supporters),
    dissenters: strList(d.dissenters),
    evidence: strList(d.evidence_refs),
    supersedes: str(d.supersedes),
    createdAt: str(d.created_at),
  };
}

export function ledgerFrom(raw: unknown): Ledger {
  const l = obj(raw);
  const claims = asArray<unknown>(l.claims).map(claimFrom);
  const held = asArray<unknown>(l.held_claims).map(claimFrom);
  return {
    sessionId: str(l.session_id),
    tasks: asArray<unknown>(l.tasks).map(taskFrom),
    claims,
    heldClaims: held.length ? held : claims.filter((c) => isActiveClaim(c.state)),
    objections: asArray<unknown>(l.objections).map(objectionFrom),
    openObjections: asArray<unknown>(l.open_objections).map(objectionFrom),
    decisions: asArray<unknown>(l.decisions).map(decisionFrom),
    readyTaskIds: strList(l.ready_task_ids),
    blockedTaskIds: strList(l.blocked_task_ids),
    blockingOpen: flag(l.blocking_open),
    counts: countMap(l.counts),
    unreadable: flag(l.unreadable),
  };
}

export function usageFrom(raw: unknown): Usage {
  const u = obj(raw);
  const scheduler = obj(u.scheduler);
  const state = obj(scheduler.state);
  return {
    sessionId: str(u.session_id),
    budgets: budgetsFrom(u.budgets),
    spent: {
      rounds: num(state.rounds),
      turns: num(state.turns),
      wallSeconds: num(state.wall_seconds),
      totalTokens: num(state.total_tokens),
      parallel: num(state.parallel),
    },
    exhausted: str(state.exhausted),
    stopReason: str(u.stop_reason) || str(scheduler.stop_reason),
    turns: num(u.turns),
    calls: num(scheduler.granted),
    waitedMs: num(scheduler.waited_ms_total),
  };
}

export function eventFrom(raw: unknown): CouncilEvent {
  const e = obj(raw);
  return {
    id: str(e.id),
    seq: num(e.seq),
    name: str(e.name),
    sessionId: str(e.session_id),
    activityId: str(e.activity_id),
    payload: obj(e.payload),
    createdAt: str(e.created_at),
  };
}

/* ── derived views, kept pure so they can be checked ────────────────────── */

/** The claim states in which a resource is still taken (`ACTIVE_CLAIM_STATES`). */
export const ACTIVE_CLAIM_STATES = ['requested', 'held', 'handoff_pending'];

export function isActiveClaim(state: string): boolean {
  return ACTIVE_CLAIM_STATES.indexOf(str(state).trim()) >= 0;
}

/**
 * Whether a seat may modify anything. Fails closed: an unknown profile is a
 * typo or a newer server talking to an older page, and both answer "no".
 */
export function canWrite(profile: string, config?: Pick<CouncilConfig, 'writingProfiles'> | null): boolean {
  const writing = config?.writingProfiles?.length ? config.writingProfiles : FALLBACK_WRITING_PROFILES;
  return writing.indexOf(str(profile).trim()) >= 0;
}

/**
 * One resource, one key. Mirrors `ledger.normalize_resource` as far as a
 * browser can: path kinds collapse separators, `.` and `..` segments and case
 * so `src/a.py`, `src\a.py` and `./src/../src/a.py` are ONE resource; every
 * other kind is an opaque identifier and is only trimmed, because running a
 * path normaliser over an artifact id would invent a filesystem meaning it
 * does not have.
 *
 * This is a READING aid for the panel: exclusion is decided on the server, by
 * the ledger and by `FileLockRegistry`. It exists so the screen groups two
 * spellings of one file into one row instead of showing two owners.
 */
export const PATH_CLAIM_KINDS = ['file', 'directory'];

export function normalizeResource(kind: string, resource: string): string {
  const raw = str(resource).trim();
  if (!raw || PATH_CLAIM_KINDS.indexOf(str(kind).trim()) < 0) return raw;
  const parts = raw.replace(/\\/g, '/').split('/');
  const out: string[] = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else out.push('..');
      continue;
    }
    out.push(part);
  }
  const lead = /^[/\\]/.test(raw) ? '/' : '';
  return (lead + out.join('/')).toLowerCase();
}

/** `kind:resource` — a file named `x` and an artifact named `x` are two things. */
export function claimKey(kind: string, resource: string): string {
  return `${str(kind).trim() || 'file'}:${normalizeResource(kind, resource)}`;
}

/**
 * Who holds this resource right now, or `''`.
 *
 * Only an ACTIVE claim owns anything: a released or expired row is history.
 * When two active rows somehow name the same key the FIRST is the holder and
 * the rest are reported by `claimConflicts` — showing the last one would make
 * a conflict look like an orderly handover.
 */
export function holderOf(claims: Claim[], kind: string, resource: string): string {
  const key = claimKey(kind, resource);
  for (const claim of claims ?? []) {
    if (!isActiveClaim(claim.state)) continue;
    if (claimKey(claim.kind, claim.resource) === key) return claim.holderId;
  }
  return '';
}

export interface ResourceRow {
  key: string;
  kind: string;
  resource: string;
  holderId: string;
  state: string;
  taskId: string;
  /** More than one active claim on the same key: a ledger that disagrees with itself. */
  contested: string[];
}

/** The claims panel: one row per resource, with who has it. */
export function resourcesHeld(claims: Claim[]): ResourceRow[] {
  const rows = new Map<string, ResourceRow>();
  for (const claim of claims ?? []) {
    if (!isActiveClaim(claim.state)) continue;
    const key = claimKey(claim.kind, claim.resource);
    const existing = rows.get(key);
    if (existing) {
      if (claim.holderId && claim.holderId !== existing.holderId) existing.contested.push(claim.holderId);
      continue;
    }
    rows.set(key, {
      key,
      kind: claim.kind,
      resource: claim.resource,
      holderId: claim.holderId,
      state: claim.state,
      taskId: claim.taskId,
      contested: [],
    });
  }
  return [...rows.values()];
}

/**
 * One turn's messages, in the order they arrived.
 *
 * A council turn is one user message and everything the room did about it, so
 * the transcript is read in turns rather than as a flat list: a critique that
 * answers a proposal three messages back belongs beside it. Messages with no
 * turn id keep their own group in the position where they first appear —
 * dropping them would hide exactly the rows nobody planned for.
 */
export interface TurnGroup {
  turnId: string;
  messages: CouncilMessage[];
  authors: string[];
}

export function groupByTurn(messages: CouncilMessage[] | null | undefined): TurnGroup[] {
  const groups: TurnGroup[] = [];
  const index = new Map<string, TurnGroup>();
  for (const message of messages ?? []) {
    if (!message) continue;
    const key = str(message.turnId);
    let group = index.get(key);
    if (!group) {
      group = { turnId: key, messages: [], authors: [] };
      index.set(key, group);
      groups.push(group);
    }
    group.messages.push(message);
    if (message.authorId && group.authors.indexOf(message.authorId) < 0) group.authors.push(message.authorId);
  }
  return groups;
}

/** Objection statuses that are still owed an answer. `accepted` is one: */
/*  agreeing with an objection is not doing the work it asks for. */
export const OPEN_OBJECTION_STATUSES = ['open', 'accepted'];

export function isOpenObjection(status: string): boolean {
  return OPEN_OBJECTION_STATUSES.indexOf(str(status).trim()) >= 0;
}

/**
 * Why this room cannot move, and why it cannot be called verified.
 *
 * Two different questions, and conflating them is how a screen ends up
 * announcing "verified" over an unanswered "this is wrong":
 *
 *  - `blocked` means work will not continue on its own: a task that is
 *    `blocked` or `failed`, or one the ledger listed in `blocked_task_ids`.
 *  - `verifiable` means nothing open forbids the word `verified`. An open
 *    `blocking` objection removes it on its own, whatever the tasks did.
 *
 * A `concern` is neither: it keeps a close `disputed` in the summary, so it
 * is reported as a caveat and never as a stop.
 */
export interface RoomBlock {
  blocked: boolean;
  verifiable: boolean;
  disputed: boolean;
  reasons: string[];
  objectionIds: string[];
  taskIds: string[];
}

export function roomBlock(ledger: Ledger | null | undefined): RoomBlock {
  const out: RoomBlock = {
    blocked: false, verifiable: true, disputed: false,
    reasons: [], objectionIds: [], taskIds: [],
  };
  if (!ledger) return out;

  const open = (ledger.openObjections?.length ? ledger.openObjections : ledger.objections ?? [])
    .filter((o) => isOpenObjection(o.status));
  const blocking = open.filter((o) => o.severity === 'blocking');
  const concerns = open.filter((o) => o.severity === 'concern');
  if (blocking.length || ledger.blockingOpen) {
    out.verifiable = false;
    out.reasons.push('blocking_objection');
    out.objectionIds.push(...blocking.map((o) => o.id).filter(Boolean));
  }
  if (concerns.length) {
    out.disputed = true;
    out.reasons.push('open_concern');
    out.objectionIds.push(...concerns.map((o) => o.id).filter(Boolean));
  }
  if ((ledger.decisions ?? []).some((d) => d.status !== 'superseded' && d.dissenters.length)) {
    out.disputed = true;
    out.reasons.push('dissent');
  }

  const stuck = (ledger.tasks ?? []).filter((t) => t.status === 'blocked' || t.status === 'failed');
  const listed = (ledger.blockedTaskIds ?? []).filter(Boolean);
  if (stuck.length || listed.length) {
    out.blocked = true;
    out.verifiable = false;
    out.reasons.push('blocked_task');
    const ids = new Set<string>([...stuck.map((t) => t.id).filter(Boolean), ...listed]);
    out.taskIds.push(...ids);
  }
  if (blocking.length || ledger.blockingOpen) out.disputed = true;
  return out;
}

/**
 * Where a reconnection resumes, without a repeat and without a hole.
 *
 * The stream numbers every event with a monotonic `seq` and `since(seq)`
 * answers strictly what follows it, so a client that stores the last seq it
 * saw gets neither that event again nor anything before it. Two things this
 * function must therefore never do: move the cursor backwards, and swallow
 * the gap marker the server inserts when its ring buffer already dropped what
 * was asked for. A silent hole costs a decision nobody knows was taken; the
 * warning costs a refresh.
 */
export interface Gap {
  missed: number;
  fromSeq: number;
  toSeq: number;
  reason: string;
}

export interface Resume {
  cursor: number;
  applied: CouncilEvent[];
  gap: Gap | null;
  duplicates: number;
}

/** A gap marker is a `council_error` whose payload says `gap`. */
export function gapOf(event: CouncilEvent | null | undefined): Gap | null {
  if (!event) return null;
  const payload = obj(event.payload);
  if (payload.gap !== true) return null;
  return {
    missed: num(payload.missed),
    fromSeq: num(payload.from_seq),
    toSeq: num(payload.to_seq),
    reason: str(payload.reason) || 'buffer_overflow',
  };
}

export function advanceCursor(cursor: number, events: CouncilEvent[] | null | undefined): Resume {
  let next = num(cursor);
  const applied: CouncilEvent[] = [];
  let gap: Gap | null = null;
  let duplicates = 0;
  const seen = new Set<string>();
  for (const event of events ?? []) {
    if (!event) continue;
    const hole = gapOf(event);
    if (hole) {
      // The marker consumes no sequence number of its own: its seq is where
      // the surviving events begin, so resuming from it asks for those and
      // never for the ones that are gone.
      if (event.seq > next) next = event.seq;
      gap = gap && gap.missed > hole.missed ? gap : hole;
      continue;
    }
    if (event.seq <= num(cursor) || seen.has(event.id || String(event.seq))) {
      duplicates += 1;
      continue;
    }
    seen.add(event.id || String(event.seq));
    applied.push(event);
    if (event.seq > next) next = event.seq;
  }
  return { cursor: next, applied, gap, duplicates };
}

/* ── verdicts and closes: a word, a tone, and never colour alone ────────── */

export type Tone = 'ok' | 'warn' | 'danger' | 'neutral';

export interface Reading {
  /** The server's own word, unedited, so a log and the screen agree. */
  value: string;
  /** The key `t()` translates. Never a colour, never an icon on its own. */
  label: string;
  tone: Tone;
  /** True only for the one word that means evidence was produced. */
  trusted: boolean;
  note: string;
}

/**
 * The five states a close may report (`synthesis.STATUSES`).
 *
 * `verified` is the only one that means something was proved, and it is the
 * only one `trusted` is true for. `decided` is a conclusion with no execution
 * behind it, `unverified` is work nothing proves, `blocked` needs a person,
 * and `disputed` is an unanswered disagreement — which outranks the rest,
 * because a close that reports progress over dissent hides the one thing the
 * reader needed.
 *
 * `/api/council/config` now sends the same five under `close_statuses`. This
 * constant stays because the five have LABELS and TONES, which the server has
 * no business deciding; the list is here so a build that added a sixth on the
 * server shows up as a status this file cannot read rather than as one it
 * renders wrongly.
 */
export const CLOSE_STATUSES = ['decided', 'verified', 'unverified', 'blocked', 'disputed'];

export function closeReading(status: unknown): Reading {
  const value = str(status).trim();
  switch (value) {
    case 'verified':
      return { value, label: 'Verified', tone: 'ok', trusted: true,
        note: 'a proof packet came back proved and every task finished well' };
    case 'decided':
      return { value, label: 'Decided', tone: 'neutral', trusted: false,
        note: 'a conclusion was reached and nothing was executed; deciding is not verifying' };
    case 'unverified':
      return { value, label: 'Unverified', tone: 'warn', trusted: false,
        note: 'work happened and nothing proves it' };
    case 'blocked':
      return { value, label: 'Blocked', tone: 'danger', trusted: false,
        note: 'something cannot continue without a person' };
    case 'disputed':
      return { value, label: 'Disputed', tone: 'danger', trusted: false,
        note: 'an objection is still open or a decision carries dissent' };
    default:
      return { value, label: value || 'No close recorded', tone: 'neutral', trusted: false,
        note: 'the room reported no close status' };
  }
}

/**
 * The four words a task's verification may end on (`adapters.VERDICTS`).
 * `verified` is `prove`'s `proved` renamed, and nothing else earns it.
 */
export function verdictReading(verdict: unknown): Reading {
  const value = str(verdict).trim();
  switch (value) {
    case 'verified':
    case 'proved':
      return { value, label: 'Verified', tone: 'ok', trusted: true,
        note: 'prove compared what was claimed with what was observed' };
    case 'partial':
      return { value, label: 'Partly proved', tone: 'warn', trusted: false,
        note: 'some of the claim held and some of it was not shown' };
    case 'unproved':
      return { value, label: 'Unproved', tone: 'warn', trusted: false,
        note: 'no evidence either way; an absence, not a refutation' };
    case 'contradicted':
      return { value, label: 'Contradicted', tone: 'danger', trusted: false,
        note: 'the evidence disagrees with the claim' };
    case 'none':
    case '':
      return { value, label: 'Not verified', tone: 'neutral', trusted: false,
        note: 'no proof packet was produced, so nothing here claims verification' };
    default:
      return { value, label: value, tone: 'neutral', trusted: false, note: '' };
  }
}

/** How loud an objection is. `blocking` is the one that removes `verified`. */
export function severityTone(severity: string): Tone {
  const value = str(severity).trim();
  if (value === 'blocking') return 'danger';
  if (value === 'concern') return 'warn';
  return 'neutral';
}

/**
 * Where a task stands.
 *
 * `verified` and `done` share a tone and do not share a meaning: `done` is what
 * the run reported, `verified` is what `prove` returned about a ChangeSet built
 * from what Faustus observed. The screen prints the word, so the reader can
 * tell them apart; the colour only says "no one is waiting on this".
 * `blocked` and `failed` need a person.
 */
export function taskTone(status: string): Tone {
  const value = str(status).trim();
  if (value === 'done' || value === 'verified') return 'ok';
  if (value === 'blocked' || value === 'failed') return 'danger';
  if (value === 'review' || value === 'running') return 'warn';
  return 'neutral';
}

/**
 * Who wrote a message, and what they are allowed to do.
 *
 * `readOnly` is a permission read off the seat's profile, never off its role
 * name: a reviewer handed `scoped_write` by an explicit authorisation writes,
 * and a driver narrowed to `read_only` does not.
 */
export interface Attribution {
  id: string;
  name: string;
  kind: string;
  roles: string[];
  readOnly: boolean;
  /** The seat is not in this room's participant list (the user, the system). */
  external: boolean;
  claimsIdentity: string;
}

export function attributionOf(
  message: CouncilMessage,
  participants: Participant[],
  config?: Pick<CouncilConfig, 'writingProfiles'> | null,
): Attribution {
  const seat = (participants ?? []).find((p) => p.id === message.authorId);
  return {
    id: message.authorId,
    name: seat?.displayName || message.authorId || message.authorKind,
    kind: message.authorKind,
    roles: seat?.roles ?? [],
    readOnly: seat ? !canWrite(seat.toolProfile, config) : true,
    external: !seat,
    claimsIdentity: message.claimsIdentity,
  };
}

/** Who a message is for. `room` is the token that means everybody. */
export function audienceOf(message: CouncilMessage, participants: Participant[]): string[] {
  const names = new Map((participants ?? []).map((p) => [p.id, p.displayName || p.id]));
  return (message.audience ?? []).map((id) => (id === 'room' ? 'room' : names.get(id) ?? id));
}

/* ── the API ────────────────────────────────────────────────────────────── */

const BASE = '/api/council';
const path = (id: string, tail = ''): string => `${BASE}/${encodeURIComponent(id)}${tail}`;

async function send<T>(url: string, method: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: body === undefined
      ? { Accept: 'application/json' }
      : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const payload = (await response.json().catch(() => ({}))) as unknown;
  const refusal = refusalOf(payload);
  if (refusal) throw new CouncilRefusal(refusal);
  if (!response.ok) {
    const detail = str(obj(payload).detail) || `${url} responded ${response.status}`;
    throw new CouncilRefusal({ code: response.status === 404 ? 'not_found' : 'command_failed', detail });
  }
  return payload as T;
}

/** The body a route wraps under one key, or the body itself. */
function unwrap(payload: unknown, key: string): unknown {
  const body = obj(payload);
  return key in body ? body[key] : body;
}

/** Everything a form needs to open a room. Read once, per screen. */
export async function loadConfig(signal?: AbortSignal): Promise<CouncilConfig> {
  return configFrom(unwrap(await getJson<unknown>(`${BASE}/config`, signal), 'config'));
}

export async function listRooms(signal?: AbortSignal): Promise<CouncilRoom[]> {
  const payload = await getJson<unknown>(BASE, signal);
  return asArray<unknown>(payload, 'sessions').map(roomFrom).filter((r) => r.id);
}

export interface RoomDetail {
  room: CouncilRoom;
  participants: Participant[];
}

/** One room and its seats, from whichever of the two shapes the route sends. */
export function roomDetailFrom(raw: unknown): RoomDetail {
  const body = obj(raw);
  const inner = obj(body.session ?? body.room);
  const source = Object.keys(inner).length ? inner : body;
  const rows = asArray<unknown>(body.participants ?? inner.participants);
  return {
    room: roomFrom(source),
    participants: rows.filter((row) => typeof row === 'object' && row !== null).map(participantFrom),
  };
}

export async function loadRoom(id: string, signal?: AbortSignal): Promise<RoomDetail> {
  return roomDetailFrom(await getJson<unknown>(path(id), signal));
}

export interface SeatRequest {
  model: string;
  endpointId?: string;
  displayName?: string;
  roles: string[];
}

export interface NewRoom {
  title: string;
  policy: string;
  participants: SeatRequest[];
  workspace?: string;
  projectId?: string;
  budgets?: Budgets;
}

/**
 * Open a room. The owner is NOT sent: it comes from the authenticated caller
 * on the server, and a room opened as somebody else is the oldest bug in any
 * multi-tenant surface. `tool_profile` is not sent either — a request cannot
 * grant a permission, and the server recomputes every seat's profile anyway.
 */
export async function createRoom(input: NewRoom): Promise<RoomDetail> {
  const payload = await send<unknown>(BASE, 'POST', {
    title: input.title,
    policy: input.policy,
    workspace: input.workspace ?? '',
    project_id: input.projectId ?? '',
    budgets: input.budgets ? budgetsPayload(input.budgets) : undefined,
    participants: input.participants.map((seat) => ({
      model: seat.model,
      endpoint_id: seat.endpointId ?? '',
      display_name: seat.displayName ?? '',
      roles: seat.roles,
    })),
  });
  return roomDetailFrom(payload);
}

/** Change a room against the revision you read (§8: two orders, one winner). */
export async function patchRoom(id: string, patch: Record<string, unknown>, revision: number): Promise<RoomDetail> {
  return roomDetailFrom(await send<unknown>(path(id), 'PATCH', { ...patch, expected_revision: revision }));
}

/** Hide a room from the list. Nothing is deleted. */
export async function archiveRoom(id: string): Promise<void> {
  await send<unknown>(path(id), 'DELETE');
}

export async function loadMessages(id: string, sinceId = '', signal?: AbortSignal): Promise<CouncilMessage[]> {
  const query = sinceId ? `?since_id=${encodeURIComponent(sinceId)}` : '';
  const payload = await getJson<unknown>(path(id, `/messages${query}`), signal);
  return asArray<unknown>(payload, 'messages').map(messageFrom);
}

/**
 * Say something to the room. Answers with a turn id and gets out of the way:
 * the work runs in the background and reports through the event stream, so a
 * dropped connection costs a reconnection and never the turn.
 *
 * `idempotencyKey` is what makes a retry land on the turn the first request
 * opened instead of paying for a second one.
 */
export async function postMessage(
  id: string, content: string, options: { mentions?: string[]; idempotencyKey?: string } = {},
): Promise<{ turnId: string; status: string }> {
  const payload = obj(await send<unknown>(path(id, '/messages'), 'POST', {
    content,
    mentions: options.mentions ?? [],
    idempotency_key: options.idempotencyKey ?? '',
  }));
  return { turnId: str(payload.turn_id), status: str(payload.status) };
}

/** The eight commands of §13. A refusal is a token, never a sentence. */
export async function command(
  id: string, name: string, args: Record<string, unknown> = {},
): Promise<Record<string, unknown>> {
  return obj(await send<unknown>(path(id, '/commands'), 'POST', { command: name, ...args }));
}

export async function loadLedger(id: string, signal?: AbortSignal): Promise<Ledger> {
  return ledgerFrom(unwrap(await getJson<unknown>(path(id, '/ledger'), signal), 'ledger'));
}

export async function loadTasks(id: string, signal?: AbortSignal): Promise<Task[]> {
  return asArray<unknown>(await getJson<unknown>(path(id, '/tasks'), signal), 'tasks').map(taskFrom);
}

export async function loadDecisions(id: string, signal?: AbortSignal): Promise<Decision[]> {
  return asArray<unknown>(await getJson<unknown>(path(id, '/decisions'), signal), 'decisions').map(decisionFrom);
}

export async function loadUsage(id: string, signal?: AbortSignal): Promise<Usage> {
  return usageFrom(unwrap(await getJson<unknown>(path(id, '/usage'), signal), 'usage'));
}

/**
 * The long poll behind the stream. `since` is the cursor, and the answer is
 * strictly what follows it — including the gap marker when the buffer already
 * dropped what was asked for.
 */
export async function waitForEvents(id: string, since: number, signal?: AbortSignal): Promise<CouncilEvent[]> {
  const payload = await getJson<unknown>(path(id, `/wait?since=${encodeURIComponent(String(since))}`), signal);
  return asArray<unknown>(payload, 'events').map(eventFrom);
}

/**
 * Follow a room live. The frames are unnamed and carry the event name inside
 * the JSON, which is the dialect the rest of Faustus speaks: a NAMED SSE
 * frame never reaches `onmessage`, and a page written against the unnamed
 * stream goes silently deaf on a named one.
 *
 * The one NAMED frame the endpoint sends is `end`, and it is not a failure:
 * the stream has a deadline and asks to be reopened from the cursor the
 * caller now holds. `onEnd` is that invitation; `onFail` is everything else,
 * so the caller can fall back to `waitForEvents`.
 */
export function followRoom(
  id: string, since: number, onEvent: (event: CouncilEvent) => void, onFail: () => void,
  onEnd?: () => void,
): () => void {
  if (typeof EventSource === 'undefined') {
    onFail();
    return () => {};
  }
  let source: EventSource | null = null;
  try {
    source = new EventSource(path(id, `/events?since=${encodeURIComponent(String(since))}`));
  } catch {
    onFail();
    return () => {};
  }
  const close = () => {
    if (!source) return;
    try {
      source.close();
    } catch {
      /* already closed */
    }
    source = null;
  };
  source.onmessage = (frame: MessageEvent<string>) => {
    try {
      onEvent(eventFrom(JSON.parse(frame.data) as unknown));
    } catch {
      /* one unreadable frame is not a dead stream */
    }
  };
  source.addEventListener('end', () => {
    close();
    if (onEnd) onEnd();
    else onFail();
  });
  source.onerror = () => {
    close();
    onFail();
  };
  return close;
}

/* ── the close ──────────────────────────────────────────────────────────── */

export interface Verification {
  verdict: string;
  sufficient: boolean;
  source: string;
  proofId: string;
  note: string;
  uncertainty: number;
}

export interface Change {
  taskId: string;
  title: string;
  status: string;
  owner: string;
  reviewer: string;
  resources: string[];
  runId: string;
  proofId: string;
}

export interface Contribution {
  participantId: string;
  displayName: string;
  roles: string[];
  messages: number;
  abstentions: number;
  types: Record<string, number>;
}

/**
 * The close of an activity, built on the server FROM THE LEDGER and never by
 * re-reading the transcript. Nothing on this side reinterprets it: the fields
 * below are rendered as they arrived, and `status` is the word the ladder in
 * `synthesis.status_of` produced.
 */
export interface CouncilClose {
  sessionId: string;
  result: string;
  status: string;
  stopReason: string;
  decisions: Decision[];
  changes: Change[];
  verification: Verification;
  openObjections: Objection[];
  contributions: Contribution[];
  usage: { inputTokens: number; outputTokens: number; totalTokens: number; calls: number; wallSeconds: number; source: string };
}

export function closeFrom(raw: unknown): CouncilClose {
  const s = obj(raw);
  const verification = obj(s.verification);
  const usage = obj(s.usage);
  return {
    sessionId: str(s.session_id),
    result: str(s.result),
    status: str(s.status),
    stopReason: str(s.stop_reason),
    decisions: asArray<unknown>(s.decisions).map(decisionFrom),
    changes: asArray<unknown>(s.changes).map((row) => {
      const ch = obj(row);
      return {
        taskId: str(ch.task_id), title: str(ch.title), status: str(ch.status),
        owner: str(ch.owner), reviewer: str(ch.reviewer),
        resources: strList(ch.resources), runId: str(ch.run_id), proofId: str(ch.proof_id),
      };
    }),
    verification: {
      verdict: str(verification.verdict) || 'none',
      sufficient: flag(verification.sufficient),
      source: str(verification.source),
      proofId: str(verification.proof_id),
      note: str(verification.note),
      uncertainty: asArray<unknown>(verification.uncertainty).length,
    },
    openObjections: asArray<unknown>(s.open_objections).map(objectionFrom),
    contributions: asArray<unknown>(s.contributions).map((row) => {
      const co = obj(row);
      return {
        participantId: str(co.participant_id),
        displayName: str(co.display_name) || str(co.participant_id),
        roles: strList(co.roles),
        messages: num(co.messages),
        abstentions: num(co.abstentions),
        types: countMap(co.types),
      };
    }),
    usage: {
      inputTokens: num(usage.input_tokens), outputTokens: num(usage.output_tokens),
      totalTokens: num(usage.total_tokens), calls: num(usage.calls),
      wallSeconds: num(usage.wall_seconds), source: str(usage.source) || 'unreported',
    },
  };
}

/**
 * Ask the room to close what it has.
 *
 * This is the only door to a full close today: `council_activity_completed`
 * carries the status and the stop reason and no read route returns the last
 * summary, so a page that reloaded after a turn finished can show the verdict
 * word and must ask again for the rest. Noted in PENDIENTES.
 */
export async function requestSynthesis(id: string): Promise<{ close: CouncilClose | null; stopReason: string; state: string }> {
  const answer = obj(await command(id, 'request_synthesis'));
  const result = obj(answer.result);
  const summary = result.summary;
  return {
    close: summary ? closeFrom(summary) : null,
    stopReason: str(result.stop_reason),
    state: str(result.state),
  };
}
