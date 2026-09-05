/**
 * Tournament (`/api/tournament`): the same prompt to N models blind and in
 * parallel, then rounds where each one sees the others' answers anonymised
 * and is told to weave a hybrid; then a judged, ranked table.
 *
 * The pure shapes and the synthesis prompt come from the previous
 * interface's tournament.js; the screen is Studio's own.
 */
import { ApiError, asArray, getJson } from './api';

export type TournamentStatus = 'queued' | 'running' | 'judging' | 'cancelling' | 'done' | 'error' | 'cancelled' | string;

export const LIVE: readonly string[] = ['queued', 'running', 'judging', 'cancelling'];
export const AXES = ['correctness', 'completeness', 'sophistication'] as const;

export interface Answer {
  entry: number;
  model: string;
  round: number;
  text: string;
  elapsedS: number | null;
  tokens: number | null;
  tokensSource: string;
}

export interface Final {
  entry: number;
  model: string;
  round: number;
  text: string;
  outcome: string;
  scores: Record<(typeof AXES)[number], number | null>;
  total: number | null;
  tiebreak: number | null;
  note: string;
  rank: number | null;
}

export interface ProgressRow {
  entry: number;
  model: string;
  round: number | null;
  state: 'queued' | 'running' | 'answered' | 'error' | 'cancelled';
  chars: number;
}

export interface TournamentEvent {
  ts: number;
  event: string;
  model: string;
  entry: number | null;
  round: number | null;
  detail: string;
}

export interface Run {
  id: string;
  status: TournamentStatus;
  error: string;
  prompt: string;
  models: string[];
  rounds: number;
  judgeModel: string;
  seed: number | null;
  created: number;
  durationS: number;
  roundsRun: number;
  stoppedBy: string;
  convergence: { score: number | null; converged: boolean; reason: string } | null;
  answers: Answer[];
  final: Final[];
  ranking: string;
  rankingNote: string;
  mergePrompt: string;
  errors: { entry: number; model: string; round: number | null; error: string }[];
  cancelled: { entry: number; model: string; round: number | null; reason: string }[];
  degraded: boolean;
  judge: { model: string; ok: boolean; attempts: number | null; error: string } | null;
  progress: ProgressRow[];
  events: TournamentEvent[];
  /** Listing rows carry these instead of a result. */
  winner: string | null;
}

export interface TournamentConfig {
  enabled: boolean;
  maxModels: number;
  minModels: number;
  maxRounds: number;
  defaultRounds: number;
  fusionInstruction: string;
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() && Number.isFinite(Number(v)) ? Number(v) : null);
const int = (v: unknown): number | null => {
  const n = num(v);
  return n === null ? null : Math.trunc(n);
};
const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v));

function answerFrom(raw: Record<string, unknown>): Answer {
  return { entry: int(raw.entry) ?? 0, model: str(raw.model), round: int(raw.round) ?? 0, text: str(raw.text), elapsedS: num(raw.elapsed_s), tokens: int(raw.tokens), tokensSource: str(raw.tokens_source) };
}

function finalFrom(raw: Record<string, unknown>): Final {
  const scores = (raw.scores && typeof raw.scores === 'object' ? raw.scores : {}) as Record<string, unknown>;
  return {
    entry: int(raw.entry) ?? 0,
    model: str(raw.model),
    round: int(raw.round) ?? 0,
    text: str(raw.text),
    outcome: str(raw.outcome),
    scores: { correctness: int(scores.correctness), completeness: int(scores.completeness), sophistication: int(scores.sophistication) },
    total: int(raw.total),
    tiebreak: num(raw.tiebreak),
    note: str(raw.note),
    rank: int(raw.rank),
  };
}

export function runFrom(raw: Record<string, unknown>): Run {
  const result = (raw.result && typeof raw.result === 'object' ? raw.result : {}) as Record<string, unknown>;
  const conv = result.convergence && typeof result.convergence === 'object' ? (result.convergence as Record<string, unknown>) : null;
  const judge = result.judge && typeof result.judge === 'object' ? (result.judge as Record<string, unknown>) : null;
  const stateOf = (s: string): ProgressRow['state'] => (s === 'running' || s === 'answered' || s === 'error' || s === 'cancelled' ? s : 'queued');
  return {
    id: str(raw.id),
    status: str(raw.status) || 'queued',
    error: str(raw.error),
    prompt: str(raw.prompt),
    models: asArray<unknown>(raw.models).map(str),
    rounds: int(raw.rounds) ?? 0,
    judgeModel: str(raw.judge_model),
    seed: int(raw.seed),
    created: num(raw.created) ?? 0,
    durationS: num(raw.duration_s) ?? 0,
    roundsRun: int(result.rounds_run) ?? 0,
    stoppedBy: str(result.stopped_by ?? raw.stopped_by),
    convergence: conv ? { score: num(conv.score), converged: Boolean(conv.converged), reason: str(conv.reason) } : null,
    answers: asArray<Record<string, unknown>>(result.answers).map(answerFrom),
    final: asArray<Record<string, unknown>>(result.final).map(finalFrom),
    ranking: str(result.ranking ?? raw.ranking),
    rankingNote: str(result.ranking_note),
    mergePrompt: str(result.merge_prompt),
    errors: asArray<Record<string, unknown>>(result.errors).map((e) => ({ entry: int(e.entry) ?? -1, model: str(e.model), round: int(e.round), error: str(e.error) })),
    cancelled: asArray<Record<string, unknown>>(result.cancelled).map((e) => ({ entry: int(e.entry) ?? -1, model: str(e.model), round: int(e.round), reason: str(e.reason) })),
    degraded: Boolean(result.degraded),
    judge: judge ? { model: str(judge.model), ok: Boolean(judge.ok), attempts: int(judge.attempts), error: str(judge.error) } : null,
    progress: asArray<Record<string, unknown>>(raw.progress).map((p) => ({ entry: int(p.entry) ?? 0, model: str(p.model), round: int(p.round), state: stateOf(str(p.state)), chars: int(p.chars) ?? 0 })),
    events: asArray<Record<string, unknown>>(raw.events).map((e) => ({ ts: num(e.ts) ?? 0, event: str(e.event), model: str(e.model), entry: int(e.entry), round: int(e.round), detail: str(e.error || e.reason || e.ranking || e.score || '') })),
    winner: typeof raw.winner === 'string' ? raw.winner : null,
  };
}

async function send(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(path, { method, credentials: 'same-origin', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let detail = '';
    try {
      detail = str(((await res.json()) as { detail?: unknown }).detail);
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `${path} responded ${res.status}`, res.status);
  }
  return (await res.json()) as Record<string, unknown>;
}

export async function tournamentConfig(signal?: AbortSignal): Promise<TournamentConfig> {
  const raw = await getJson<Record<string, unknown>>('/api/tournament/config', signal);
  return { enabled: Boolean(raw.enabled), maxModels: int(raw.max_models) ?? 8, minModels: int(raw.min_models) ?? 2, maxRounds: int(raw.max_rounds) ?? 6, defaultRounds: int(raw.default_rounds) ?? 3, fusionInstruction: str(raw.fusion_instruction) };
}

export async function listRuns(signal?: AbortSignal): Promise<{ runs: Run[]; enabled: boolean }> {
  const raw = await getJson<Record<string, unknown>>('/api/tournament?limit=50', signal);
  return { runs: asArray<Record<string, unknown>>(raw.runs).map(runFrom), enabled: raw.enabled !== false };
}

export async function getRun(id: string, signal?: AbortSignal): Promise<Run> {
  return runFrom(await getJson<Record<string, unknown>>(`/api/tournament/${encodeURIComponent(id)}`, signal));
}

export async function startTournament(body: { prompt: string; models: string[]; rounds: number; judgeModel?: string; seed?: number | null }): Promise<Run> {
  const payload: Record<string, unknown> = { prompt: body.prompt, models: body.models, rounds: body.rounds };
  if (body.judgeModel) payload.judge_model = body.judgeModel;
  if (body.seed !== undefined && body.seed !== null) payload.seed = body.seed;
  return runFrom(await send('/api/tournament', 'POST', payload));
}

export async function cancelTournament(id: string): Promise<void> {
  await send(`/api/tournament/${encodeURIComponent(id)}/cancel`, 'POST');
}

/**
 * Follows a live run: SSE on `/events?stream=1` (unnamed frames are the
 * events, a named `end` frame closes it) plus a slow poll. Every event
 * triggers a fresh GET so the board always draws the server's own summary
 * rather than a reconstruction. Resolves with the final run.
 */
export function followRun(id: string, onRun: (run: Run) => void, signal?: AbortSignal): Promise<Run | null> {
  return new Promise((resolve) => {
    let settled = false;
    let timer: number | null = null;
    let source: EventSource | null = null;
    const finish = (run: Run | null) => {
      if (settled) return;
      settled = true;
      if (timer) window.clearTimeout(timer);
      source?.close();
      resolve(run);
    };
    let fetching = false;
    let again = false;
    const refresh = async (): Promise<Run | null> => {
      if (fetching) {
        again = true;
        return null;
      }
      fetching = true;
      try {
        const run = await getRun(id, signal);
        onRun(run);
        if (!LIVE.includes(run.status)) finish(run);
        return run;
      } catch (e) {
        if ((e as ApiError).status === 404) finish(null);
        return null;
      } finally {
        fetching = false;
        if (again && !settled) {
          again = false;
          void refresh();
        }
      }
    };
    const poll = () => {
      if (settled || signal?.aborted) return;
      void refresh().finally(() => {
        if (!settled) timer = window.setTimeout(poll, 4000);
      });
    };
    try {
      source = new EventSource(`/api/tournament/${encodeURIComponent(id)}/events?stream=1`);
    } catch {
      source = null;
    }
    if (!source) {
      poll();
      return;
    }
    source.onmessage = () => void refresh();
    source.addEventListener('end', () => {
      source?.close();
      source = null;
      void refresh();
    });
    source.onerror = () => {
      source?.close();
      source = null;
    };
    // The stream says when something happened; the poll keeps the clock and
    // the progress rows honest while a slow model says nothing for a minute.
    poll();
    signal?.addEventListener('abort', () => finish(null));
  });
}

/* ── pure helpers (ported) ── */

export function answersByEntry(run: Run): Map<number, Answer[]> {
  const out = new Map<number, Answer[]>();
  for (const a of run.answers) {
    if (!out.has(a.entry)) out.set(a.entry, []);
    out.get(a.entry)!.push(a);
  }
  for (const rows of out.values()) rows.sort((a, b) => a.round - b.round);
  return out;
}

export function winnerOf(run: Run): Final | null {
  for (const row of run.final) if (row.rank === 1) return row;
  return run.final[0] ?? null;
}

export function stateOfEntry(run: Run, entry: number, rows: Answer[]): ProgressRow['state'] {
  for (const p of run.progress) if (p.entry === entry) return p.state;
  for (const e of run.errors) if (e.entry === entry) return 'error';
  for (const c of run.cancelled) if (c.entry === entry) return 'cancelled';
  return rows.length ? 'answered' : 'queued';
}

export function detailOfEntry(run: Run, entry: number): string {
  for (const e of run.errors) if (e.entry === entry) return e.error || 'failed';
  for (const c of run.cancelled) if (c.entry === entry) return c.reason || 'stopped';
  return '';
}

/** The synthesis prompt for the composer: the server's, or the same thing rebuilt here. */
export function mergePromptFor(run: Run, fusion: string): string {
  if (run.mergePrompt.trim()) return run.mergePrompt;
  const usable = run.final.filter((row) => row.text.trim());
  if (!usable.length) return '';
  const lines = ['Here are the final answers from a model tournament on this task.', '', 'The task was:', '', run.prompt, ''];
  usable.forEach((row, i) => {
    const label = String.fromCharCode(65 + (i % 26));
    const rank = row.rank === null ? '' : ` (ranked ${row.rank}${row.total === null ? '' : `, judged ${row.total}/300`})`;
    lines.push(`--- Solution ${label}${rank} ---`, row.text, '');
  });
  lines.push(fusion || 'Take the best ideas from all of them where they are complementary, not conflicting, and weave a hybrid that is better than any single one.');
  lines.push('Write the final answer. Where the solutions conflict, pick one and say why in a line.');
  return lines.join('\n');
}

const KEY = 'fs-tournament-setup';
export interface Setup {
  prompt: string;
  models: string[];
  rounds: number;
  judge: string;
}
export function loadSetup(): Partial<Setup> {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Partial<Setup>) : {};
  } catch {
    return {};
  }
}
export function saveSetup(s: Setup): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* private mode */
  }
}
