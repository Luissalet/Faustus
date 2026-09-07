import type { RunStatus } from '../../components';
import {
  summaryFrom,
  toolEventsFrom,
  type AskUser,
  type ChatEvent,
  type ContextLedger,
  type HarnessCheck,
  type HarnessSummary,
  type StepDiff,
  type SubagentPayload,
  type Todo,
  type TurnMetrics,
  type WebSource,
} from '../../adapters/chat';
import type { Attachment } from '../../adapters/composer';
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

export interface Turn {
  id: string;
  /** The message's database id, when the server has one; edits need it. */
  dbId?: string;
  /** Position in the server's history when loaded from it; truncation
   *  counts server messages, and the list may hide some (approval prompts). */
  historyIndex?: number;
  role: 'user' | 'assistant';
  text: string;
  thinking: string;
  steps: Step[];
  rounds: number;
  metrics?: TurnMetrics;
  sources: WebSource[];
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
  research?: { phase: string; round: number; totalSources: number; message: string; startedAt: number; avgDuration: number; done: boolean };
  /** Decoding speed measured from the stream, while it is still arriving. */
  live?: LiveRate;
  error?: string;
  edited?: boolean;
  /** The reliability harness: what it checked, and what really happened. */
  checks: HarnessCheck[];
  summary?: HarnessSummary;
  todos?: Todo[];
  plan?: string;
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
      return event.thinking
        ? { ...turn, thinking: turn.thinking + event.text, live: liveToken(live, now, true) }
        : { ...turn, text: turn.text + event.text, live: liveToken(live, now, false) };
    case 'heartbeat': {
      const phase: LiveRate['phase'] =
        event.phase === 'thinking' || event.phase === 'writing' || event.phase === 'tool'
          ? event.phase
          : 'waiting';
      const label = phase === 'tool'
        ? [event.tool.replace(/_/g, ' '), event.detail].filter(Boolean).join(' · ')
        : event.phase === 'research' && event.detail
          ? event.detail
          : undefined;
      const next = livePhase(live, now, phase, label);
      return {
        ...turn,
        rounds: Math.max(turn.rounds, event.round),
        live: { ...next, phaseAt: event.phaseAt > 0 ? event.phaseAt : next.phaseAt },
      };
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
        docId: event.docId,
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
    case 'research':
      return { ...turn, research: { phase: event.phase, round: event.round, totalSources: event.totalSources, message: event.message, startedAt: event.startedAt ? event.startedAt * 1000 : turn.research?.startedAt || Date.now(), avgDuration: event.avgDuration || turn.research?.avgDuration || 0, done: false } };
    case 'image':
      return { ...turn, images: [...turn.images, event.url] };
    case 'fallback':
      return {
        ...turn,
        note: t('{model} did not answer; {other} answered instead.', { model: event.selected || t('The chosen model'), other: event.answeredBy }),
      };
    case 'terminal':
      return event.failed ? { ...turn, error: event.message ?? t('The model has failed.') } : turn;
    case 'error':
      return { ...turn, error: event.message };
    case 'progress':
      return { ...turn, todos: event.todos };
    case 'plan':
      return { ...turn, plan: event.plan };
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
    case 'done':
      return {
        ...turn,
        streaming: false,
        steps: turn.steps.map((step) => (step.state === 'running' ? { ...step, state: 'cancelled' } : step)),
        workers: turn.workers.map((w) => (workerLive(w) ? { ...w, status: 'partial' as const, stopReason: w.stopReason || t('no signal') } : w)),
      };
  }
}

/**
 * What history keeps of an agent turn, back into the turn: the tool rail
 * (`tool_events`, with diffs, screenshots and sub-agent records), the
 * harness card (`harness`), web sources and an approval still pending.
 * The legacy renderer rebuilds the same things from the same fields.
 */
export function restoreFromMetadata(turn: Turn, meta: Record<string, unknown>): Turn {
  const events = toolEventsFrom(meta);
  const speaker = typeof meta.group_model === 'string' && meta.group_model ? meta.group_model : undefined;
  if (!events.length && !meta.harness && !meta.web_sources && !meta.research_sources) return speaker ? { ...turn, speaker } : turn;
  const steps: Step[] = [];
  const workers: Worker[] = [];
  let ask: AskUser | undefined;
  let approval: Turn['approval'];
  let rounds = turn.rounds;
  for (const ev of events) {
    const parked = ev.exitCode === null && /^Waiting for an exact user approval/i.test(ev.output.trim());
    const ok = ev.exitCode === null || ev.exitCode === 0;
    const pending = parked && ev.ask !== undefined && !ev.askResolved;
    const superseded = ev.askResolved && ev.askDecision === 'superseded';
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
    });
    rounds = Math.max(rounds, ev.round);
    ev.subagents.forEach((sa, i) => workers.push(workerFromPersisted(sa, i)));
    if (ev.ask && !ev.askResolved) ask = ev.ask;
    // Already answered: keep the record, not a card. The question stayed in
    // the message's own text as well, and left alone it comes back as a
    // bubble asking for a permission that was granted minutes ago.
    if (ev.ask && ev.askResolved) approval = { question: ev.ask.question, decision: ev.askDecision ?? '' };
  }
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
