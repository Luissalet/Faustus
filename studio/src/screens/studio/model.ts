import type { RunStatus } from '../../components';
import {
  summaryFrom,
  toolEventsFrom,
  type AskUser,
  type ChatEvent,
  type ContextLedger,
  type GitPolicyEvent,
  type HarnessCheck,
  type HarnessSummary,
  type StepDiff,
  type SubagentPayload,
  type Todo,
  type TurnMetrics,
  type WebSource,
  type ContextReceipt,
} from '../../adapters/chat';
import type { EvidenceRef } from '../../adapters/evidence';
import type { Attachment } from '../../adapters/composer';
import type { VramBlocked } from '../../adapters/vramAdmission';
import { t } from '../../i18n';

/**
 * The transcript's data model and the one reducer that applies a stream
 * event to the assistant turn being written. Pure: no React, no fetch, so
 * it can be reasoned about (and tested) as a function of events.
 */

export interface Step {
  id: string;
  tool: string;
  label: string;
  state: RunStatus;
  meta?: string;
  command?: string;
  output?: string;
  round: number;
  /** A file write/edit as the server diffed it. */
  diff?: StepDiff;
  /** Validated raster data: URL (desktop_screenshot, browser tools). */
  screenshot?: string;
  /** The living document this call created or changed. */
  docId?: string;
  /** CALL-03: what `_validate_native_tool_call` (src/agent_loop.py) found in
   *  this call's arguments — every error, whether or not it blocked
   *  execution, matching `src/tool_schemas.py`'s `ArgumentError` fields. */
  argumentErrors?: { field: string; kind: string; detail: string }[];
  /** CALL-03: the bounded, same-meaning repairs actually applied before the
   *  call ran (`repair_tool_arguments`'s `applied_repairs`) — one entry per
   *  field, so the tool card can show "original → corrección" next to it. */
  repairs?: { field: string; from: unknown; to: unknown; reason: string }[];
  /** Lote 50 (BENCH-03 wiring): `EvidenceRef`s the tool call attached to its
   *  result, forwarded from `ChatEvent['tool_output'].evidenceRefs` — lets a
   *  tool card offer a "ver evidencia" button per ref (`screens/Evidence.tsx`). */
  evidenceRefs?: EvidenceRef[];
  /** EXEC-01: where this actually ran (`src/native_env.py`'s
   *  `{kind, cwd, shell}`), forwarded from
   *  `ChatEvent['tool_output'].executionTarget` — see that field's doc
   *  comment in adapters/chat.ts for why it stays undefined until
   *  agent_loop.py forwards it. */
  executionTarget?: { kind: string; cwd?: string; shell?: string };
  /** OBS-01: this call's id, forwarded from `ChatEvent['tool_output'].callId`
   *  (live) or the persisted `tool_events[i].call_id` (history) — lets the
   *  tool card offer a "Ver traza" link to `/activity?trace=<callId>`. */
  callId?: string;
}

/** RES-01: one subquestion's coverage, from `DeepResearcher._coverage_snapshot`
 *  (src/deep_research.py) via the `analyzing` progress event's `coverage`
 *  list — the schema a research report is built against, each node marked
 *  by how well the findings gathered so far actually answer it. */
export interface CoverageItem {
  question: string;
  status: 'pending' | 'insufficient' | 'covered';
  matchedSources: number;
}

function coverageFromRaw(raw: unknown): CoverageItem[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const items = raw
    .map((entry) => (entry && typeof entry === 'object' ? (entry as Record<string, unknown>) : null))
    .filter((entry): entry is Record<string, unknown> => entry !== null)
    .map((entry) => ({
      question: s(entry.question),
      status: (entry.status === 'covered' || entry.status === 'insufficient' ? entry.status : 'pending') as CoverageItem['status'],
      matchedSources: n(entry.matched_sources) ?? 0,
    }))
    .filter((item) => item.question);
  return items.length ? items : undefined;
}

/**
 * One structured plan step (TASK-01), as the backend's `plan_state.Plan`
 * serializes it inside `plan_update.steps`. Since CALL-07/TASK-04, `chat.ts`'s
 * live SSE mapping (`case 'plan_update'`) carries these fields through too,
 * so a turn still in progress renders the same structured card as a turn
 * restored from history (which reads the raw persisted `tool_events` entry
 * directly — see `planUpdateFromMeta` below).
 */
export interface PlanStepView {
  id: string;
  title: string;
  status: 'pending' | 'done' | 'blocked';
  dependsOn: string[];
  evidenceRefs: string[];
  notes: string;
  verified: boolean;
}

/** One sub-agent (delegate_agents worker), folded from its stream events —
 *  the legacy board's state, field for field, so nothing it showed is lost. */
export interface Worker {
  id: string;
  delegation: string;
  index: number | null;
  name: string;
  role: string;
  model: string;
  files: string[];
  instruction: string;
  instructionFull: string;
  sessionId: string;
  status: 'queued' | 'running' | 'done' | 'failed' | 'stopped' | 'partial';
  firstSeen: number;
  startedLocal: number | null;
  startedAt: number | null;
  endedAt: number | null;
  endedLocal: number | null;
  lastEventAt: number;
  sawTick: boolean;
  tickElapsed: number | null;
  tickAt: number | null;
  round: number | null;
  maxRounds: number | null;
  rounds: number | null;
  toolCalls: number;
  failedCalls: number;
  lastTool: string;
  lastCmd: string;
  lastToolOk: boolean | null;
  lastOut: string;
  tail: string;
  toolElapsed: number | null;
  toolInFlight: boolean;
  inTok: number | null;
  outTok: number | null;
  idleS: number | null;
  stalled: boolean;
  stallReason: string;
  stallAt: number | null;
  timeoutS: number | null;
  steers: { text: string; source: string; at: number; local?: boolean }[];
  supervisor: { action: string; reason: string }[];
  note: string;
  error: string;
  stopReason: string;
  finalText: string;
  mutations: string[];
  durationS: number | null;
  stopRequested: boolean;
}

/** CMP-09/CMP-12 (W3-A): what `strategy_policy.choose_strategy` decided for
 *  THIS turn — the same shape `docs/api/strategy.md`'s `Strategy` documents,
 *  plus the active `profile`/`recipeId`. Set once, on the live `strategy`
 *  event (round 1 only) or restored from `metadata.strategy`; a turn from a
 *  server that predates either stays `undefined`, same fallback story as
 *  every other optional field on `Turn`. */
export interface TurnStrategy {
  method: string;
  profile: string;
  recipeId: string | null;
  reasons: string[];
  steps: string[];
  budget: Record<string, unknown>;
}

export interface Turn {
  id: string;
  /** The message's database id, when the server has one; edits need it. */
  dbId?: string;
  /** Position in the server's history when loaded from it; truncation
   *  counts server messages, and the list may hide some (approval prompts). */
  historyIndex?: number;
  /** F3 (CONTRATO_CABLES2): `'system'` is a condensed-range summary row —
   *  see `condensed` below and `adapters/chat.ts::loadHistory`'s own doc
   *  comment for why it is the only `system` row that ever reaches here. */
  role: 'user' | 'assistant' | 'system';
  text: string;
  thinking: string;
  steps: Step[];
  rounds: number;
  metrics?: TurnMetrics;
  sources: WebSource[];
  /** CMP-04: why each piece of context entered this turn (persisted as
   *  `metadata.context_receipts`); the transcript shows them as a card. */
  contextReceipts?: ContextReceipt[];
  /** CMP-09/CMP-12 (W3-A): see `TurnStrategy`'s doc comment. */
  strategy?: TurnStrategy;
  images: string[];
  attachments: Attachment[];
  ask?: AskUser;
  /** A permission gate of this turn that was already answered: the record
   *  of the decision, so a reload does not read as a question nobody can
   *  answer any more. */
  approval?: { question: string; decision: string };
  note?: string;
  /** Group chat transcript: who said this (metadata.group_model). */
  speaker?: string;
  /** Deep Research running before the answer: the phase it is in. */
  research?: {
    phase: string; round: number; totalSources: number; message: string; startedAt: number;
    avgDuration: number; done: boolean;
    /** RES-01: the schema's coverage as of the latest `analyzing` event —
     *  kept even once `done`, so the map is still readable once writing
     *  starts. */
    coverage?: CoverageItem[];
  };
  /** Decoding speed measured from the stream, while it is still arriving. */
  live?: LiveRate;
  /** The VRAM gate is waiting for someone to choose what to unload (OBJ-1). */
  vram?: VramBlocked;
  error?: string;
  /** OBS-03 dotted taxonomy code for `error` (`src/contracts/errors.py`'s
   *  `ERROR_CATEGORIES`, e.g. `transport.llm_service_error`) — lets a
   *  screen show the right action (retry, resume, change model, ask for
   *  permission) instead of the raw provider message. Read defensively
   *  (see `errorTraceFields` below): the backend has sent `error_class` on
   *  every `event: error` SSE chunk since CALL-06 (`llm_core.py`'s
   *  `_stream_error_chunk`), but `adapters/chat.ts`'s `ChatEvent` union
   *  does not carry it through yet, so this stays undefined until that is
   *  extended — a client on an older build simply never sets it. */
  errorClass?: string;
  /** Correlates this turn with the unified activity timeline (ACT-01) and
   *  with server logs, once a backend sends one. Same fallback story as
   *  `errorClass`. */
  traceId?: string;
  /** The step within the run this trace/error belongs to, when the server
   *  narrows it that far. Same fallback story as `errorClass`. */
  stepId?: string;
  /** ACT-06: this turn was restored from a state that no longer matches
   *  what the server holds now (a stale approval, a plan re-planned after
   *  it was displayed) — the "incompatible version" screen state, not a
   *  network failure. Same fallback story as `errorClass`. */
  versionMismatch?: boolean;
  /** UX-02/TASK-03: this turn reconnected to an outcome the client does not
   *  know yet (`ChatEvent['uncertain']`) — cleared the moment anything else
   *  arrives (a delta, a terminal event, done), since all of those mean the
   *  question is answered one way or another. */
  uncertain?: boolean;
  /** MOD-06: a mid-task fallback switched to a model that does not announce
   *  every capability the previous one did (`ChatEvent['capabilities_changed']`,
   *  `recompute_capabilities_on_model_switch` in src/agent_loop.py). Sticks
   *  for the rest of the turn — it is a fact about what happened, not a
   *  transient status. */
  capabilitiesChanged?: { fromModel: string; toModel: string; lost: string[] };
  edited?: boolean;
  /** F1 (CONTRATO_CABLES2): this OWN assistant reply was written against an
   *  earlier version of one or more wires (`GET .../stale-turns`'s per-turn
   *  view, `Studio.tsx` maps it onto turns by `historyIndex`) — never set by
   *  `apply()`/`restoreFromMetadata` (those know nothing about wires), only
   *  patched in from outside once the session's stale-turns map loads. */
  staleWires?: { wireId: string; label: string }[];
  /** F3: this row is a condensed-range summary (`role === 'system'`,
   *  `metadata.condensed === true`) — `from`/`to` are 0-based history
   *  indices (the same range `POST .../condense` took), `count` the number
   *  of original turns it replaced. Set by `Studio.tsx::turnsFromHistory`
   *  from the row's own metadata, not by `apply()` (a condensed row is
   *  never live-streamed, only ever restored from history). */
  condensed?: { from: number; to: number; count: number };
  /** OBJ-4/Lote 83: every `git_policy` event this turn's workspace produced
   *  (branch created, commit made, push sent — or one skipped/failed),
   *  in arrival order. Same accumulate-in-place pattern as `checks` below. */
  gitPolicy: GitPolicyEvent[];
  /** The reliability harness: what it checked, and what really happened. */
  checks: HarnessCheck[];
  summary?: HarnessSummary;
  todos?: Todo[];
  plan?: string;
  /** The same plan, structured (TASK-01) — set on history restore and, since
   *  CALL-07/TASK-04, on a live `plan_update` too (see `apply()`'s `'plan'`
   *  case and chat.ts's `decode()`). */
  planSteps?: PlanStepView[];
  planRevision?: number;
  planWarnings?: string[];
  contextPercent?: number;
  /** Where the context went, when the server says (`context_ledger`). */
  ledger?: ContextLedger;
  /** Sub-agents of this turn's delegate_agents calls, in arrival order. */
  workers: Worker[];
  streaming: boolean;
}

/* ── What the turn is doing right now, and how fast ── */

/**
 * A turn in flight, measured in the browser.
 *
 * Two things were invisible while a turn ran: **what** it was doing between
 * two tool calls (the rail showed three finished reads and then nothing, so
 * a live turn looked identical to a dead one) and **how fast** it was going
 * (the speed only existed in the footer, once it no longer mattered).
 *
 * The speed is measured the only way a browser can: the gaps between the
 * chunks that arrive. It is an estimate and it says so with a `~` — one
 * chunk is one token in every backend we serve, but not a promise — and the
 * server's own figure (`tokens_per_second`, taken from the backend's decode
 * timings when it reports them) replaces it in the footer when the turn ends.
 */
export interface LiveRate {
  /** Chunks received: near enough to output tokens to show with a `~`. */
  tokens: number;
  /** The last few gaps between chunks, in ms: the speed is their average. */
  recent: number[];
  /** When the last chunk arrived. */
  lastTokenAt: number;
  /** When anything at all last happened: the sign of life. */
  lastAt: number;
  /** What it is doing right now, and since when. */
  phase: 'waiting' | 'thinking' | 'writing' | 'tool';
  phaseAt: number;
  /** The tool's label, while the phase is a tool. */
  label?: string;
}

/** Gaps outside this range are not decoding: a replayed buffer arrives with
 *  no gap at all, and anything slower is a tool, a prefill or a queue. */
const GAP_MIN_MS = 3;
const GAP_MAX_MS = 4_000;
/** Enough to be steady, short enough to still be "right now". */
const RECENT = 40;

export function newLive(now: number): LiveRate {
  return { tokens: 0, recent: [], lastTokenAt: 0, lastAt: now, phase: 'waiting', phaseAt: now };
}

export function beginApproval(turn: Turn, now = Date.now()): Turn {
  return { ...turn, streaming: true, error: undefined, live: newLive(now) };
}

export function closeApproval(turn: Turn, decision: string): Turn {
  if (turn.ask?.kind !== 'tool_approval') return turn;
  const question = turn.ask.question.trim();
  let text = turn.text.trimEnd();
  if (question && text.endsWith(question)) text = text.slice(0, -question.length).trimEnd();
  const superseded = decision === 'superseded';
  return { ...turn, ask: undefined, approval: { question, decision },
    text: text ? `${text}\n\n` : '', summary: undefined,
    steps: superseded ? turn.steps.map(step => step.state === 'waiting'
      ? { ...step, state: 'cancelled' as const, meta: t('Cancelled') } : step) : turn.steps };
}

/** Tokens per second right now, or null while there is nothing honest to say. */
export function liveTps(live: LiveRate | undefined): number | null {
  if (!live || live.recent.length < 3) return null;
  const mean = live.recent.reduce((a, b) => a + b, 0) / live.recent.length;
  return mean > 0 ? 1000 / mean : null;
}

/** One chunk of generated text: counts it and times the gap before it. */
export function liveToken(live: LiveRate, now: number, thinking: boolean): LiveRate {
  const gap = live.lastTokenAt ? now - live.lastTokenAt : 0;
  const recent =
    gap >= GAP_MIN_MS && gap <= GAP_MAX_MS ? [...live.recent, gap].slice(-RECENT) : live.recent;
  const phase = thinking ? 'thinking' : 'writing';
  return {
    ...live,
    tokens: live.tokens + 1,
    recent,
    lastTokenAt: now,
    lastAt: now,
    phase,
    phaseAt: live.phase === phase ? live.phaseAt : now,
  };
}

/** Anything else that happened: a new phase, or just a sign of life. */
export function livePhase(live: LiveRate, now: number, phase: LiveRate['phase'], label?: string): LiveRate {
  const same = live.phase === phase && live.label === label;
  return { ...live, lastAt: now, phase, label, phaseAt: same ? live.phaseAt : now };
}

/** Reads `trace_id`/`step_id`/`error_class`/`version_mismatch` off any raw
 *  event-shaped object, without requiring `ChatEvent` to declare them.
 *
 *  These fields already travel over the wire on `event: error` SSE chunks
 *  (`llm_core.py`'s `_stream_error_chunk`/`_stream_status_error_chunk`) and
 *  are exactly what OBS-03/ACT-01 need client-side, but `adapters/chat.ts`'s
 *  `decode()` does not forward them onto `ChatEvent` yet (a separate lote
 *  owns that file). Reading them here as an optional passthrough, rather
 *  than waiting for the type to widen, means the day `decode()` is extended
 *  to forward them this starts populating `Turn.errorClass` etc. with no
 *  further change on this side — "consume it if it is there; fall back to
 *  what is already on the turn if it is not". */
function errorTraceFields(event: unknown): {
  errorClass?: string; traceId?: string; stepId?: string; versionMismatch?: boolean;
} {
  const raw = (event && typeof event === 'object' ? event : {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined);
  // chat.ts decodes these to camelCase; a raw SSE frame (history restore,
  // an older adapter) still carries snake_case. Read both.
  return {
    errorClass: str(raw.errorClass) ?? str(raw.error_class),
    traceId: str(raw.traceId) ?? str(raw.trace_id),
    stepId: str(raw.stepId) ?? str(raw.step_id),
    versionMismatch: raw.versionMismatch === true || raw.version_mismatch === true ? true : undefined,
  };
}

let counter = 0;
export const uid = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`;

export function blankTurn(role: Turn['role'], text = ''): Turn {
  return {
    id: uid(role),
    role,
    text,
    thinking: '',
    steps: [],
    rounds: 1,
    sources: [],
    images: [],
    attachments: [],
    gitPolicy: [],
    checks: [],
    workers: [],
    streaming: role === 'assistant',
    live: role === 'assistant' ? newLive(Date.now()) : undefined,
  };
}

/* ── Sub-agents ── */

export function newWorker(id: string, delegation: string, now: number): Worker {
  return {
    id, delegation, index: null, name: '', role: 'worker', model: '', files: [], instruction: '', instructionFull: '',
    sessionId: '', status: 'running', firstSeen: now, startedLocal: null, startedAt: null, endedAt: null, endedLocal: null,
    lastEventAt: now, sawTick: false, tickElapsed: null, tickAt: null, round: null, maxRounds: null, rounds: null,
    toolCalls: 0, failedCalls: 0, lastTool: '', lastCmd: '', lastToolOk: null, lastOut: '', tail: '', toolElapsed: null,
    toolInFlight: false, inTok: null, outTok: null, idleS: null, stalled: false, stallReason: '', stallAt: null,
    timeoutS: null, steers: [], supervisor: [], note: '', error: '', stopReason: '', finalText: '', mutations: [],
    durationS: null, stopRequested: false,
  };
}

const s = (v: unknown): string => (v === undefined || v === null ? '' : String(v));
const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() && Number.isFinite(Number(v)) ? Number(v) : null);

/** CALL-03 passthrough for a tool call's argument-validation outcome —
 *  `{"errors": [...], "repairs": [...]}`, the exact shape
 *  `_validate_native_tool_call`'s `meta` (src/agent_loop.py) attaches to
 *  `tool_output_data`/the persisted `tool_events[i]` entry, field names
 *  straight from `src/tool_schemas.py`'s `ArgumentError`/
 *  `repair_tool_arguments`. Same defensive-read idiom as `errorTraceFields`
 *  above: `adapters/chat.ts`'s `decode()` does not forward `argument_errors`/
 *  `repairs` onto the live `tool_output` `ChatEvent` yet (a fichero ajeno to
 *  this lote — see the report's "Cambios necesarios en ficheros ajenos"),
 *  so today this only ever finds something on history restore, where
 *  `restoreFromMetadata` reads the raw persisted event directly; a client
 *  that gains it on the wire needs no further change here. */
function argumentRepairFields(raw: unknown): { argumentErrors?: Step['argumentErrors']; repairs?: Step['repairs']; evidenceRefs?: Step['evidenceRefs'] } {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const errsRaw = Array.isArray(r.argumentErrors) ? r.argumentErrors : Array.isArray(r.argument_errors) ? r.argument_errors : undefined;
  const repsRaw = Array.isArray(r.repairs) ? r.repairs : undefined;
  const obj = (v: unknown) => (v && typeof v === 'object' ? (v as Record<string, unknown>) : null);
  const errors = errsRaw
    ?.map(obj)
    .filter((e): e is Record<string, unknown> => e !== null)
    .map((e) => ({ field: s(e.field), kind: s(e.kind), detail: s(e.detail) }))
    .filter((e) => e.field);
  const repairs = repsRaw
    ?.map(obj)
    .filter((rr): rr is Record<string, unknown> => rr !== null)
    .map((rr) => ({ field: s(rr.field), from: rr.from, to: rr.to, reason: s(rr.reason) }))
    .filter((rr) => rr.field);
  const refsRaw = Array.isArray(r.evidenceRefs) ? r.evidenceRefs : Array.isArray(r.evidence_refs) ? r.evidence_refs : undefined;
  const evidenceRefs = refsRaw
    ?.map(obj)
    .filter((e): e is Record<string, unknown> => e !== null && typeof e.evidence_id === 'string' && Boolean(e.evidence_id));
  return {
    argumentErrors: errors && errors.length ? errors : undefined,
    repairs: repairs && repairs.length ? repairs : undefined,
    evidenceRefs: evidenceRefs && evidenceRefs.length ? (evidenceRefs as unknown as EvidenceRef[]) : undefined,
  };
}

/** Fold one `subagent` payload into a worker (pure; the legacy _saApply). */
export function applyWorker(prev: Worker, sa: SubagentPayload, now: number): Worker {
  const w: Worker = { ...prev, steers: prev.steers.slice(), supervisor: prev.supervisor.slice(), lastEventAt: now };
  if (sa.name) w.name = s(sa.name);
  if (sa.role) w.role = s(sa.role);
  if (n(sa.index) !== null) w.index = n(sa.index);
  if (sa.session_id) w.sessionId = s(sa.session_id);
  if (sa.model) w.model = s(sa.model);
  if (Array.isArray(sa.files)) w.files = sa.files.map(String);
  if (sa.instruction) w.instruction = s(sa.instruction);
  if (sa.instruction_full) w.instructionFull = s(sa.instruction_full);
  if (n(sa.max_rounds) !== null) w.maxRounds = n(sa.max_rounds);
  if (n(sa.timeout_s) !== null) w.timeoutS = n(sa.timeout_s);
  if ((n(sa.started_at) ?? 0) > 0) w.startedAt = n(sa.started_at);
  if ((n(sa.ended_at) ?? 0) > 0) w.endedAt = n(sa.ended_at);
  if (n(sa.input_tokens) !== null) w.inTok = n(sa.input_tokens);
  if (n(sa.output_tokens) !== null) w.outTok = n(sa.output_tokens);
  if (n(sa.rounds) !== null) w.rounds = n(sa.rounds);
  switch (sa.event) {
    case 'queued':
      w.status = 'queued';
      w.note = sa.reason ? s(sa.reason) : t('waiting for a slot on the GPU');
      break;
    case 'started':
      w.status = 'running';
      w.startedLocal = now;
      w.note = '';
      break;
    case 'round':
      if (n(sa.round) !== null) w.round = n(sa.round);
      w.stalled = false;
      break;
    case 'tool':
      w.stalled = false;
      if (sa.tool) w.lastTool = s(sa.tool);
      if (sa.phase === 'start') {
        w.lastCmd = s(sa.command);
        w.lastToolOk = null;
        w.lastOut = '';
        w.tail = '';
        w.toolElapsed = null;
        w.toolInFlight = true;
      } else if (sa.phase === 'progress') {
        w.toolInFlight = true;
        if (sa.tail !== undefined && sa.tail !== null) w.tail = s(sa.tail);
        if (n(sa.elapsed_s) !== null) w.toolElapsed = n(sa.elapsed_s);
      } else {
        w.toolInFlight = false;
        w.toolCalls += 1;
        if (sa.ok === false) w.failedCalls += 1;
        w.lastToolOk = sa.ok !== false;
        w.lastOut = s(sa.output);
        w.tail = '';
        w.toolElapsed = null;
      }
      break;
    case 'tick':
      w.sawTick = true;
      if (n(sa.elapsed_s) !== null) {
        w.tickElapsed = n(sa.elapsed_s);
        w.tickAt = now;
      }
      if (n(sa.round) !== null) w.round = n(sa.round);
      if (sa.last_tool) w.lastTool = s(sa.last_tool);
      if (n(sa.tool_calls) !== null) w.toolCalls = Math.max(w.toolCalls, n(sa.tool_calls) ?? 0);
      if (n(sa.idle_s) !== null) w.idleS = n(sa.idle_s);
      if (sa.stalled) {
        if (!w.stalled) w.stallAt = now;
        w.stalled = true;
        w.stallReason = s(sa.stall_reason);
      } else {
        w.stalled = false;
      }
      break;
    case 'steer': {
      const text = s(sa.text);
      const source = s(sa.source) || 'user';
      const last = w.steers[w.steers.length - 1];
      if (last && last.text === text && last.source === source && last.local && now - last.at < 60000) {
        w.steers[w.steers.length - 1] = { ...last, local: false };
      } else {
        w.steers.push({ text, source, at: now });
      }
      break;
    }
    case 'supervisor':
      w.supervisor.push({ action: s(sa.action), reason: s(sa.reason) });
      break;
    case 'harness': {
      const reasons = Array.isArray(sa.reasons) ? sa.reasons.map(String) : [];
      w.note = `🛡 ${s(sa.status)}${reasons.length ? ': ' + reasons.join(', ') : ''}`;
      break;
    }
    case 'guard':
      w.note = `⚠ ${s(sa.kind) || 'guard'}`;
      break;
    case 'error':
      w.status = 'failed';
      w.error = s(sa.message) || 'error';
      break;
    case 'done': {
      const stopped = sa.stop_reason === 'stopped';
      const ok = !sa.error && sa.stop_reason === 'complete';
      w.status = sa.error ? 'failed' : ok ? 'done' : stopped ? 'stopped' : 'partial';
      w.stopReason = s(sa.stop_reason);
      w.error = sa.error ? s(sa.error) : w.error;
      w.finalText = s(sa.final_text);
      w.mutations = Array.isArray(sa.mutations) ? sa.mutations.map(String) : w.mutations;
      if (n(sa.tool_calls) !== null) w.toolCalls = n(sa.tool_calls) ?? 0;
      if (n(sa.failed_calls) !== null) w.failedCalls = n(sa.failed_calls) ?? 0;
      if (n(sa.duration_s) !== null) w.durationS = n(sa.duration_s);
      if (!w.endedAt) w.endedLocal = now;
      w.stalled = false;
      w.tail = '';
      w.toolInFlight = false;
      if (Array.isArray(sa.steered)) {
        for (const item of sa.steered) {
          const text = typeof item === 'string' ? item : s((item as Record<string, unknown>)?.text);
          const source = typeof item === 'string' ? 'user' : s((item as Record<string, unknown>)?.source) || 'user';
          if (text && !w.steers.some((x) => x.text === text)) w.steers.push({ text, source, at: now });
        }
      }
      if (Array.isArray(sa.supervisor) && !w.supervisor.length) {
        w.supervisor = sa.supervisor.map((x) =>
          typeof x === 'string' ? { action: x, reason: '' } : { action: s((x as Record<string, unknown>)?.action), reason: s((x as Record<string, unknown>)?.reason) },
        );
      }
      break;
    }
    default:
      break;
  }
  return w;
}

export const workerLive = (w: Worker) => w.status === 'queued' || w.status === 'running';

/** A worker from the record history keeps (`tool_events[i].subagents[j]`). */
export function workerFromPersisted(sa: SubagentPayload, i: number): Worker {
  const w = applyWorker(newWorker(s(sa.id ?? sa.session_id ?? i), s(sa.delegation), 0), { ...sa, event: 'done' }, 0);
  w.index = n(sa.index) ?? i;
  if (sa.stop_reason === undefined && !sa.error && sa.status === 'done') w.status = 'done';
  // Older records carry the reviewer's role only in its name.
  if (!sa.role && /^reviewer$/i.test(w.name)) w.role = 'reviewer';
  return w;
}

/** A tool name the model uses → the words a person reads on the rail. */
const TOOL_WORDS: Record<string, string> = {
  bash: 'Terminal',
  python: 'Python',
  read_file: 'Read',
  write_file: 'Write',
  edit_file: 'Edit',
  apply_patch: 'Patch',
  ls: 'List',
  glob: 'Find files',
  grep: 'Search in files',
  web_search: 'Search the web',
  web_fetch: 'Open URL',
  fetch_url: 'Open URL',
  browser: 'Browser',
  create_document: 'Create document',
  edit_document: 'Edit document',
  update_document: 'Update document',
  generate_image: 'Generate image',
  delegate_agents: 'Delegate',
  ask_user: 'Ask',
  update_plan: 'Plan',
  todowrite: 'Tasks',
  manage_memory: 'Memory',
};

export function stepLabel(tool: string, command: string): string {
  const word = TOOL_WORDS[tool] ? t(TOOL_WORDS[tool]) : tool.replace(/_/g, ' ');
  const brief = command.trim().split('\n')[0].slice(0, 96);
  return brief ? `${word} · ${brief}` : word;
}

export function formatMetrics(m: TurnMetrics): string {
  const parts: string[] = [];
  if (m.model) parts.push(m.model);
  if (m.outputTokens !== undefined) parts.push(`${m.outputTokens} tok`);
  // A backend that reports its decode timings gives the speed the model was
  // actually writing at. Without them the server divides the tokens by the
  // whole turn — prefill, tools and all — and calling that "tok/s" next to a
  // live meter reading fifty times more is how a number stops being believed.
  if (m.tokensPerSecond !== undefined) {
    parts.push(
      m.tpsSource === 'computed'
        ? t('{n} tok/s over the whole turn', { n: m.tokensPerSecond.toFixed(1) })
        : `${m.tokensPerSecond.toFixed(1)} tok/s`,
    );
  }
  if (m.responseTime !== undefined) parts.push(`${m.responseTime.toFixed(1)} s`);
  if (m.contextPercent !== undefined) parts.push(`${t('context')} ${Math.round(m.contextPercent)}%`);
  return parts.join(' · ');
}

function lastRunning(steps: Step[], tool: string): number {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].state === 'running' && steps[i].tool === tool) return i;
  }
  return -1;
}

/**
 * Applies one stream event to the assistant turn at the end of the list.
 *
 * Every event also feeds the live meter (`turn.live`): what the turn is
 * doing and how fast. A turn between two tool calls emits nothing the
 * transcript used to draw, and silence is exactly what a dead turn looks
 * like, so "nothing to draw" is itself worth drawing.
 */
export function apply(turn: Turn, event: ChatEvent): Turn {
  const now = Date.now();
  const live = turn.live ?? newLive(now);
  switch (event.type) {
    case 'delta':
      // UX-02/TASK-03: real content arriving settles "uncertain" one way —
      // the turn is plainly alive and answering.
      return event.thinking
        ? { ...turn, thinking: turn.thinking + event.text, live: liveToken(live, now, true), uncertain: undefined }
        : { ...turn, text: turn.text + event.text, live: liveToken(live, now, false), uncertain: undefined };
    case 'heartbeat': {
      const phase: LiveRate['phase'] =
        event.phase === 'thinking' || event.phase === 'writing' || event.phase === 'tool'
          ? event.phase
          : 'waiting';
      const label = phase === 'tool'
        ? [event.tool.replace(/_/g, ' '), event.detail].filter(Boolean).join(' · ')
        : event.detail
          ? event.detail
          : undefined;
      const next = livePhase(live, now, phase, label);
      return {
        ...turn,
        rounds: Math.max(turn.rounds, event.round),
        live: { ...next, phaseAt: event.phaseAt > 0 ? event.phaseAt : next.phaseAt },
      };
    }
    case 'vram': {
      // The gate before the first call: the ticket stays on the turn while
      // it is blocked (Studio shows the dialog), and any later phase —
      // unloading, a warning, or the turn simply going on — clears it.
      const blocked = event.phase === 'vram_blocked' ? event.blocked : undefined;
      const label = event.phase === 'vram_blocked'
        ? t('No room in VRAM — waiting for you to choose what to unload')
        : event.phase === 'unloading_model'
          ? event.message || t('Unloading models to make room')
          : event.message || undefined;
      return { ...turn, vram: blocked, live: livePhase(live, now, 'waiting', label) };
    }
    case 'tool_start': {
      const label = stepLabel(event.tool, event.command);
      const busy = livePhase(live, now, 'tool', label);
      // After an approval the server replays the same tool's start: the
      // step that was waiting becomes the one that runs, not a twin.
      const held = turn.steps.findIndex((s) => s.state === 'waiting' && s.tool === event.tool);
      if (held !== -1) {
        const steps = turn.steps.slice();
        steps[held] = { ...steps[held], state: 'running', meta: undefined };
        return { ...turn, steps, live: busy };
      }
      return {
        ...turn,
        live: busy,
        rounds: Math.max(turn.rounds, event.round),
        steps: [
          ...turn.steps,
          {
            id: uid('step'),
            tool: event.tool,
            label,
            state: 'running',
            command: event.fullCommand ?? event.command,
            round: event.round,
          },
        ],
      };
    }
    case 'tool_progress': {
      const index = lastRunning(turn.steps, event.tool);
      // Still the same tool, but this is the proof it is alive: a long bash
      // says so every two seconds and nothing else does.
      const ticking = livePhase(live, now, 'tool', live.label);
      if (index === -1) return { ...turn, live: ticking };
      const steps = turn.steps.slice();
      steps[index] = { ...steps[index], meta: event.message.slice(0, 60) };
      return { ...turn, steps, live: ticking };
    }
    case 'tool_output': {
      const index = lastRunning(turn.steps, event.tool);
      // CALL-03: not on `ChatEvent['tool_output']` yet (see
      // `argumentRepairFields`'s doc comment) — reads as absent until
      // `decode()` forwards it, same as every other passthrough field here.
      const repairFields = argumentRepairFields(event);
      const finished: Step = {
        id: index === -1 ? uid('step') : turn.steps[index].id,
        tool: event.tool,
        label: index === -1 ? stepLabel(event.tool, event.command) : turn.steps[index].label,
        state: event.exitCode === null || event.exitCode === 0 ? 'succeeded' : 'failed',
        meta: event.exitCode !== null && event.exitCode !== 0 ? `exit ${event.exitCode}` : undefined,
        command: index === -1 ? event.command : turn.steps[index].command,
        output: event.output,
        round: index === -1 ? turn.rounds : turn.steps[index].round,
        diff: event.diff,
        screenshot: event.screenshot,
        argumentErrors: repairFields.argumentErrors ?? (index === -1 ? undefined : turn.steps[index].argumentErrors),
        repairs: repairFields.repairs ?? (index === -1 ? undefined : turn.steps[index].repairs),
        docId: event.docId,
        evidenceRefs: event.evidenceRefs ?? (index === -1 ? undefined : turn.steps[index].evidenceRefs),
        executionTarget: event.executionTarget ?? (index === -1 ? undefined : turn.steps[index].executionTarget),
        callId: event.callId ?? (index === -1 ? undefined : turn.steps[index].callId),
      };
      const steps = turn.steps.slice();
      if (index === -1) steps.push(finished);
      else steps[index] = finished;
      // The tool is done and the model has been asked again: from here until
      // its first chunk nothing arrives, and that silence is the stretch that
      // used to look like a hung turn.
      return { ...turn, steps, live: livePhase(live, now, 'waiting') };
    }
    case 'round':
      return { ...turn, rounds: Math.max(turn.rounds, event.round), live: livePhase(live, now, 'waiting') };
    case 'ask_user': {
      // The tool that needs permission is either still running or was just
      // closed by the server with an empty output (some approval paths emit
      // tool_output before asking). Either way it is the last step, and it
      // must read as "waiting" so the replayed tool_start reuses it.
      let steps = turn.steps.map((s) => (s.state === 'running' ? { ...s, state: 'waiting' as const } : s));
      if (steps.length && !steps.some((s) => s.state === 'waiting')) {
        const last = steps[steps.length - 1];
        if (!last.output) steps = [...steps.slice(0, -1), { ...last, state: 'waiting' as const, meta: undefined }];
      }
      return { ...turn, ask: event.ask, steps };
    }
    case 'ask_resolved':
      // The steps keep their "waiting" look until the replayed tool_start
      // turns them back into "running": only the card goes.
      return turn.ask ? { ...turn, ask: undefined } : turn;
    case 'metrics':
      return { ...turn, metrics: { ...turn.metrics, ...event.metrics } };
    case 'sources':
      return { ...turn, sources: event.sources, research: turn.research ? { ...turn.research, done: true } : turn.research };
    case 'context_receipts':
      return { ...turn, contextReceipts: event.receipts };
    case 'strategy':
      return {
        ...turn,
        strategy: {
          method: event.method, profile: event.profile, recipeId: event.recipeId,
          reasons: event.reasons, steps: event.steps, budget: event.budget,
        },
      };
    case 'research':
      return {
        ...turn,
        research: {
          phase: event.phase, round: event.round, totalSources: event.totalSources, message: event.message,
          startedAt: event.startedAt ? event.startedAt * 1000 : turn.research?.startedAt || Date.now(),
          avgDuration: event.avgDuration || turn.research?.avgDuration || 0, done: false,
          coverage: coverageFromRaw(event.coverage) ?? turn.research?.coverage,
        },
      };
    case 'image':
      return { ...turn, images: [...turn.images, event.url] };
    case 'fallback':
      return {
        ...turn,
        note: t('{model} did not answer; {other} answered instead.', { model: event.selected || t('The chosen model'), other: event.answeredBy }),
      };
    case 'terminal': {
      if (!event.failed) return { ...turn, uncertain: undefined };
      const fields = errorTraceFields(event);
      return {
        ...turn, error: event.message ?? t('The model has failed.'),
        errorClass: fields.errorClass ?? turn.errorClass,
        traceId: fields.traceId ?? turn.traceId,
        stepId: fields.stepId ?? turn.stepId,
        versionMismatch: fields.versionMismatch ?? turn.versionMismatch,
        uncertain: undefined,
      };
    }
    case 'error': {
      const fields = errorTraceFields(event);
      return {
        ...turn, error: event.message,
        errorClass: fields.errorClass ?? turn.errorClass,
        traceId: fields.traceId ?? turn.traceId,
        stepId: fields.stepId ?? turn.stepId,
        versionMismatch: fields.versionMismatch ?? turn.versionMismatch,
      };
    }
    case 'progress':
      return { ...turn, todos: event.todos };
    case 'plan': {
      // CALL-07/TASK-04 glue: the live SSE `plan_update` decode (chat.ts)
      // now carries `steps`/`revision`/`warnings` the same way a restored
      // history turn does (planUpdateFromMeta below) — reuse the same
      // per-step parser so a plan card looks identical whether it arrived
      // live or via history restore.
      const steps = event.steps
        ? event.steps.map(planStepFromRaw).filter((x): x is PlanStepView => x !== null)
        : undefined;
      return {
        ...turn,
        plan: event.plan,
        planSteps: steps ?? turn.planSteps,
        planRevision: event.revision ?? turn.planRevision,
        planWarnings: event.warnings ?? turn.planWarnings,
      };
    }
    case 'check':
      return { ...turn, checks: [...turn.checks, event.check] };
    case 'summary':
      return { ...turn, summary: event.summary };
    case 'context':
      return { ...turn, contextPercent: event.percent ?? turn.contextPercent, ledger: event.ledger ?? turn.ledger };
    case 'subagent': {
      const sa = event.payload;
      const id = s(sa.id ?? sa.session_id);
      if (!id) return turn;
      const delegation = s(sa.delegation);
      const now = Date.now();
      const workers = turn.workers.slice();
      const at = workers.findIndex((w) => w.id === id && (!delegation || !w.delegation || w.delegation === delegation));
      if (at === -1) workers.push(applyWorker(newWorker(id, delegation, now), sa, now));
      else workers[at] = applyWorker(workers[at], sa, now);
      return { ...turn, workers };
    }
    // Frames and documents belong to the side panel, not to the turn.
    case 'frame':
    case 'doc_open':
    case 'doc_delta':
    case 'doc_update':
    case 'doc_suggestions':
      return turn;
    // UX-02/TASK-03: the outbox reconnected to a turn whose true outcome is
    // not known yet — say so plainly rather than leaving the last visible
    // state (often nothing at all, for a brand-new reconnect) to be read as
    // either "still going" or "it's done".
    case 'uncertain':
      return { ...turn, uncertain: true };
    // MOD-06: sticks for the rest of the turn once it has happened.
    case 'capabilities_changed':
      return {
        ...turn,
        capabilitiesChanged: { fromModel: event.fromModel, toModel: event.toModel, lost: event.lost },
      };
    case 'git_policy':
      return { ...turn, gitPolicy: [...turn.gitPolicy, event.event] };
    case 'done':
      return {
        ...turn,
        streaming: false,
        uncertain: undefined,
        steps: turn.steps.map((step) => (step.state === 'running' ? { ...step, state: 'cancelled' } : step)),
        workers: turn.workers.map((w) => (workerLive(w) ? { ...w, status: 'partial' as const, stopReason: w.stopReason || t('no signal') } : w)),
      };
  }
}

export function planStepFromRaw(raw: unknown): PlanStepView | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const title = s(r.title);
  if (!title) return null;
  const status: PlanStepView['status'] = r.status === 'done' || r.status === 'blocked' ? r.status : 'pending';
  const strings = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => s(x)).filter(Boolean) : []);
  return {
    id: s(r.id) || uid('plan-step'),
    title,
    status,
    dependsOn: strings(r.depends_on),
    evidenceRefs: strings(r.evidence_refs),
    notes: s(r.notes),
    verified: r.verified !== false,
  };
}

/**
 * TASK-01 / QA-38: `plan_update` was never saved before — only streamed live
 * — so reloading a session lost the docked plan window entirely. The server
 * now persists the tool event's `plan_update` field (src/agent_loop.py, the
 * same place `ask_user` is persisted); this reads it back directly off the
 * raw `tool_events` array rather than through `toolEventsFrom` (adapters/
 * chat.ts), which strips `plan_update` down to a bare markdown string for
 * the live `ChatEvent` union. Restoring from history therefore can show the
 * structured steps; a turn still streaming live only has `plan` (the
 * markdown), same as before this existed — see `PlanStepView`'s doc comment.
 * The LAST plan_update in the turn wins, same as the live reducer (`case
 * 'plan'`), which always replaces the whole plan rather than merging.
 */
export function planUpdateFromMeta(meta: Record<string, unknown>):
  { plan: string; steps?: PlanStepView[]; revision?: number; warnings?: string[] } | undefined {
  const raw = Array.isArray(meta.tool_events) ? (meta.tool_events as Record<string, unknown>[]) : [];
  let found: Record<string, unknown> | undefined;
  for (const ev of raw) {
    if (ev && typeof ev === 'object' && ev.plan_update && typeof ev.plan_update === 'object') {
      found = ev.plan_update as Record<string, unknown>;
    }
  }
  if (!found) return undefined;
  const plan = s(found.plan);
  if (!plan) return undefined;
  const steps = Array.isArray(found.steps)
    ? (found.steps as unknown[]).map(planStepFromRaw).filter((x): x is PlanStepView => x !== null)
    : undefined;
  const revision = n(found.revision) ?? undefined;
  const warnings = Array.isArray(found.warnings)
    ? (found.warnings as unknown[]).map((x) => s(x)).filter(Boolean)
    : undefined;
  return { plan, steps, revision, warnings };
}

/** CMP-09/CMP-12 (W3-A): `metadata.strategy`, persisted by `src/agent_loop.py`
 *  the same way `metadata.context_receipts` already is (see that field's own
 *  handling below) — `undefined` when the message predates this, or when
 *  round 1 never computed a strategy for it (no owner/task text). */
function strategyFromMeta(meta: Record<string, unknown>): TurnStrategy | undefined {
  const raw = meta.strategy;
  if (!raw || typeof raw !== 'object') return undefined;
  const r = raw as Record<string, unknown>;
  const method = s(r.method);
  if (!method) return undefined;
  return {
    method,
    profile: s(r.profile),
    recipeId: typeof r.recipe_id === 'string' && r.recipe_id ? r.recipe_id : null,
    reasons: Array.isArray(r.reasons) ? r.reasons.map((x) => s(x)).filter(Boolean) : [],
    steps: Array.isArray(r.steps) ? r.steps.map((x) => s(x)).filter(Boolean) : [],
    budget: r.budget && typeof r.budget === 'object' ? (r.budget as Record<string, unknown>) : {},
  };
}

/**
 * What history keeps of an agent turn, back into the turn: the tool rail
 * (`tool_events`, with diffs, screenshots and sub-agent records), the
 * harness card (`harness`), web sources and an approval still pending.
 * The legacy renderer rebuilds the same things from the same fields.
 */
export function restoreFromMetadata(turn: Turn, meta: Record<string, unknown>): Turn {
  const events = toolEventsFrom(meta);
  const planUpdate = planUpdateFromMeta(meta);
  const speaker = typeof meta.group_model === 'string' && meta.group_model ? meta.group_model : undefined;
  if (!events.length && !meta.harness && !meta.web_sources && !meta.research_sources && !meta.context_receipts && !meta.strategy) return speaker ? { ...turn, speaker } : turn;
  // CALL-03: `toolEventsFrom` (adapters/chat.ts) strips `argument_errors`/
  // `repairs` down to nothing, the same way it used to strip `plan_update`
  // before `planUpdateFromMeta` started reading it straight off the raw
  // array below — same fix, same reason: `events[i]` and `rawEvents[i]` are
  // the same persisted `tool_events` entry, 1:1 in order.
  const rawEvents = Array.isArray(meta.tool_events) ? (meta.tool_events as Record<string, unknown>[]) : [];
  const steps: Step[] = [];
  const workers: Worker[] = [];
  let ask: AskUser | undefined;
  let approval: Turn['approval'];
  let rounds = turn.rounds;
  events.forEach((ev, idx) => {
    const parked = ev.exitCode === null && /^Waiting for an exact user approval/i.test(ev.output.trim());
    const ok = ev.exitCode === null || ev.exitCode === 0;
    const pending = parked && ev.ask !== undefined && !ev.askResolved;
    const superseded = ev.askResolved && ev.askDecision === 'superseded';
    const repairFields = argumentRepairFields(rawEvents[idx]);
    steps.push({
      id: uid('step'),
      tool: ev.tool,
      label: stepLabel(ev.tool, ev.command),
      state: superseded ? 'cancelled' : pending ? 'waiting' : parked ? 'cancelled' : ok ? 'succeeded' : 'failed',
      meta: superseded ? t('Cancelled') : pending ? t('permission requested') : parked ? (ev.askResolved ? t('permission answered') : t('permission requested')) : !ok ? `exit ${ev.exitCode}` : undefined,
      command: ev.command,
      output: parked ? '' : ev.output,
      round: ev.round,
      diff: ev.diff,
      screenshot: ev.screenshot,
      docId: ev.docId,
      argumentErrors: repairFields.argumentErrors,
      repairs: repairFields.repairs,
      evidenceRefs: repairFields.evidenceRefs,
      callId: ev.callId,
    });
    rounds = Math.max(rounds, ev.round);
    ev.subagents.forEach((sa, i) => workers.push(workerFromPersisted(sa, i)));
    if (ev.ask && !ev.askResolved) ask = ev.ask;
    // Already answered: keep the record, not a card. The question stayed in
    // the message's own text as well, and left alone it comes back as a
    // bubble asking for a permission that was granted minutes ago.
    if (ev.ask && ev.askResolved) approval = { question: ev.ask.question, decision: ev.askDecision ?? '' };
  });
  const harness = meta.harness && typeof meta.harness === 'object' ? (meta.harness as Record<string, unknown>) : null;
  const rawSources = Array.isArray(meta.web_sources) ? meta.web_sources : Array.isArray(meta.research_sources) ? meta.research_sources : null;
  const sources = rawSources
    ? (rawSources as Record<string, unknown>[])
        .map((x) => ({ title: s(x.title) || s(x.url), url: s(x.url) }))
        .filter((x) => x.url)
    : turn.sources;
  // The server appends the gate's question to the message's own text. Once
  // the gate is answered that sentence is the whole message, and reading it
  // back as prose is how a finished decision looks like a frozen chat.
  const text =
    approval && turn.text.trim() === approval.question.trim() ? '' : turn.text;
  return {
    ...turn,
    text,
    speaker,
    steps: steps.length ? steps : turn.steps,
    workers: workers.length ? workers : turn.workers,
    rounds: Math.max(rounds, Math.max(0, Math.trunc(n(harness?.round_count) ?? 0))),
    ask: ask ?? (approval ? undefined : turn.ask),
    approval: approval ?? turn.approval,
    summary: approval?.decision === 'superseded' && !ask ? undefined : harness ? summaryFrom(harness) : turn.summary,
    sources,
    contextReceipts: Array.isArray(meta.context_receipts)
      ? (meta.context_receipts as Record<string, unknown>[]).map((x) => ({ source: s(x.source), kind: s(x.kind), ref: s(x.ref), why: s(x.why) }))
      : turn.contextReceipts,
    strategy: strategyFromMeta(meta) ?? turn.strategy,
    plan: planUpdate?.plan ?? turn.plan,
    planSteps: planUpdate?.steps ?? turn.planSteps,
    planRevision: planUpdate?.revision ?? turn.planRevision,
    planWarnings: planUpdate?.warnings ?? turn.planWarnings,
  };
}

/** The user's text as it was typed, without the file blocks the server
 *  inlines for the model (the legacy renderer strips the same markers). */
export function cleanUserText(text: string, hasAttachments: boolean): string {
  let out = text.replace(
    /\n*\[Image: [^\]]+\]\n[\s\S]*?(?=\n*\[Image: |\n*\[Image attached: |\n*=== File: |\n*\[PDF content\]:|$)/g,
    '',
  );
  if (hasAttachments) {
    out = out
      .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+```[\s\S]*?```/g, '')
      .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+[\s\S]*?(?=\n*=== File:|$)/g, '')
      .replace(/\n*\[PDF content\]:[\s\S]*?(?=\n*\[PDF content\]|\n*=== File:|$)/g, '')
      .replace(/\n*\[Image attached: [^\]]+\]/g, '')
      .replace(/\n*\[Attached (?:document|non-text) file\]/g, '');
  }
  return out.replace(/\s*\[\d+ attachment\(s\)\]$/, '').trim();
}
