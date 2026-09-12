import { t } from '../i18n';

/**
 * Lote B (CONTRATO_INF04.md) — the local-inference benchmark, a thin typed
 * mirror of Lote A's backend: `src/contracts/inference.py`'s
 * `InferenceProfile`/`BenchmarkRun`/`Comparison` shapes, `src/bench/
 * {runner,profiles,suites}.py`'s logic, `routes/benchmark_routes.py`'s
 * routes and `docs/api/bench.md`'s own account of what the numbers do and
 * do not prove. Field names are kept exactly as the server writes them
 * (`to_dict()`), the same choice `adapters/sideThreads.ts` made — this
 * adapter never re-implements a rule the server already owns: the budget
 * math, the promotion gate, the comparability check all stay server-side.
 *
 * Errors: every route here answers a failure with the FLAT
 * `{"error": str, "error_class": "bench.<reason>"}` body
 * `routes/benchmark_routes.py` documents — read directly, never through
 * FastAPI's `detail` (see `SideThreadsApiError`'s own note for why).
 *
 * §01/§09's one non-negotiable rule for this screen: opening it, or a
 * remount/reconnect once a run exists, must NEVER call `startBench` on its
 * own — only an explicit click does. Nothing in this module calls
 * `startBench` itself; `Optimize.tsx` is the only caller, and
 * `tests/test_inf04_bench_js.py` greps that it never does so from inside a
 * `useEffect`.
 */

import { ApiError, responseReason } from './api';

// ── vocabularies (mirrors src/contracts/inference.py) ───────────────────────

export type ProfileObjective = 'interactive' | 'coding_agent' | 'long_documents';
export type ProfileEvaluation = 'not_evaluated' | 'baseline' | 'evaluated' | 'inconclusive' | 'recommended' | 'regression';
export type ProfileSource = 'manual' | 'current' | 'candidate' | 'imported';
export type CheckKind = 'contains' | 'regex' | 'json_valid' | 'json_has_keys' | 'max_words' | 'language_es' | 'no_tool_leak';
export type RunState =
  | 'planned' | 'waiting_resources' | 'preparing' | 'running' | 'evaluating'
  | 'completed' | 'partial' | 'cancelled' | 'interrupted' | 'failed';
export type ComparisonVerdict = 'improvement' | 'no_change' | 'regression' | 'inconclusive';

/** States `GET /runs/{id}` can still be moving through — the runner's own
 *  `RUN_STATES_IN_FLIGHT`. `Optimize.tsx` polls on a 2s timer exactly while
 *  the run it is watching is in one of these; a terminal state stops the
 *  timer instead of leaving it to poll a run that will never change again. */
export const RUN_STATES_IN_FLIGHT: readonly RunState[] = ['waiting_resources', 'preparing', 'running', 'evaluating'];

export function isRunInFlight(state: RunState): boolean {
  return (RUN_STATES_IN_FLIGHT as readonly string[]).includes(state);
}

const RUN_STATE_LABELS: Record<RunState, string> = {
  planned: 'Planned',
  waiting_resources: 'Waiting for resources',
  preparing: 'Preparing',
  running: 'Running',
  evaluating: 'Evaluating',
  completed: 'Completed',
  partial: 'Partial (budget exhausted)',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted',
  failed: 'Failed',
};

export function runStateLabel(state: RunState): string {
  return t(RUN_STATE_LABELS[state] ?? state);
}

export function runStateTone(state: RunState): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (state === 'completed') return 'ok';
  if (state === 'failed' || state === 'interrupted') return 'danger';
  if (state === 'partial' || state === 'cancelled') return 'warning';
  return 'neutral';
}

// ── shapes (src/contracts/inference.py's `to_dict()`, unchanged) ───────────

export interface ModelDescriptor {
  artifact_id: string;
  revision: string | null;
  digest: string | null;
  architecture: string | null;
  kind: 'dense' | 'moe' | 'unknown';
  quantization: string | null;
  total_params: number | null;
  active_params: number | null;
  mtp: boolean | null;
  identity_state: 'confirmed' | 'provisional';
}

export interface EngineIdentity {
  implementation: string;
  version: string | null;
  build: string | null;
  platform: string | null;
  host: string | null;
  port: number | null;
  managed: 'faustus' | 'external' | 'remote';
  generation: number;
  session_id: string | null;
}

export interface InferenceProfile {
  id: string;
  label: string;
  model: ModelDescriptor;
  engine: EngineIdentity;
  hardware_id: string | null;
  options: Record<string, unknown>;
  objective: ProfileObjective;
  evaluation: ProfileEvaluation;
  fingerprint: string;
  created_at: string;
  source: ProfileSource;
}

export interface BenchmarkCaseCheck {
  kind: CheckKind;
  arg: unknown;
}

export interface BenchmarkCase {
  id: string;
  suite: string;
  prompt: string | null;
  messages: Record<string, unknown>[] | null;
  checks: BenchmarkCaseCheck[];
  max_tokens: number | null;
  tags: string[];
}

export interface Suite {
  id: string;
  version: string;
  objective: ProfileObjective;
  cases: BenchmarkCase[];
}

export interface RunBudget {
  max_cases: number | null;
  max_seconds: number | null;
  max_generated_tokens: number | null;
  repeats: number;
}

export interface RunConditions {
  cold_start: boolean | null;
  resident: boolean | null;
  prefix_cache: string | null;
  seed: number | null;
  temperature: number | null;
  sampling: Record<string, unknown>;
}

/** `MetricValue`/`ExecutionMetrics` off the wire, INF-03's exact shape —
 *  passed straight to `adapters/chat.ts::executionMetricsFrom` (never
 *  re-parsed here) so `ExecutionTimeline` renders a sample's per-phase
 *  breakdown the same way it renders a chat turn's. */
export type BenchMetricValue = { value: number | null; source: string };
export interface BenchExecutionMetrics {
  schema_version: number;
  phases: Record<string, BenchMetricValue>;
  tokens: Record<string, BenchMetricValue>;
  scope: string;
  engine: EngineIdentity | null;
  observed_at: string | null;
  notes: string[];
}

export interface SampleQuality {
  passed: boolean | null;
  failed_checks: string[];
}

export interface RunSample {
  case_id: string;
  repeat: number;
  metrics: BenchExecutionMetrics | null;
  quality: SampleQuality;
  output_chars: number | null;
  error: string | null;
}

export interface RunStat {
  median: number | null;
  p95: number | null;
  n: number;
}

export interface RunSummary {
  cases_run: number;
  cases_planned: number;
  quality_pass_rate: number | null;
  gen_tps: RunStat;
  ttft_ms: RunStat;
  total_ms: number | null;
  /** `null` — never a guessed number — until this exact model's decode
   *  speed has been learned by a first run. `estimateLabel` below is the
   *  one place this becomes text. */
  estimate_seconds: number | null;
}

export interface RunInterruption {
  at: string | null;
  reason: string;
}

export interface BenchmarkRun {
  id: string;
  suite_id: string;
  suite_version: string;
  profile: InferenceProfile;
  baseline_run_id: string | null;
  state: RunState;
  budget: RunBudget;
  conditions: RunConditions;
  samples: RunSample[];
  summary: RunSummary;
  interruptions: RunInterruption[];
  started_at: string | null;
  finished_at: string | null;
  notes: string[];
}

export interface ComparisonDeltas {
  gen_tps_median_pct: number | null;
  ttft_median_pct: number | null;
  quality_pass_rate_delta: number | null;
}

export interface SampleSizes {
  baseline: number;
  candidate: number;
}

export interface Comparison {
  baseline_run_id: string;
  candidate_run_id: string;
  verdict: ComparisonVerdict;
  reasons: string[];
  deltas: ComparisonDeltas;
  sample_sizes: SampleSizes;
  comparable: boolean;
}

// ── transport ────────────────────────────────────────────────────────────

export class BenchApiError extends ApiError {
  readonly errorClass: string | null;

  constructor(message: string, status: number, errorClass: string | null) {
    super(message, status);
    this.name = 'BenchApiError';
    this.errorClass = errorClass;
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
    const flatMessage = typeof payload.error === 'string' && payload.error.trim() ? payload.error : null;
    const message = flatMessage ?? (await responseReason(response, path));
    throw new BenchApiError(message, response.status, errorClass);
  }
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}), signal });
}

// ── routes (routes/benchmark_routes.py) ─────────────────────────────────────

export function listSuites(signal?: AbortSignal): Promise<Suite[]> {
  return get<{ suites: Suite[] }>('/api/bench/suites', signal).then((d) => d.suites);
}

export interface ProfilesFor {
  current: InferenceProfile | null;
  saved: InferenceProfile[];
}

/** `GET /api/bench/profiles?endpoint=&model=` — `current` is only filled
 *  in when both `endpoint`/`model` are given (the running configuration
 *  right now); `saved` always lists every profile ever saved, regardless
 *  of which endpoint/model a caller asked about (`routes/benchmark_routes.
 *  py`'s own doc comment: this is admin-scoped evidence, not per-caller). */
export function getProfiles(endpoint: string, model: string, signal?: AbortSignal): Promise<ProfilesFor> {
  const p = new URLSearchParams();
  if (endpoint) p.set('endpoint', endpoint);
  if (model) p.set('model', model);
  return get<ProfilesFor>(`/api/bench/profiles?${p.toString()}`, signal);
}

export interface PlanBudgetInput {
  maxCases?: number;
  maxSeconds?: number;
  maxGeneratedTokens?: number;
  repeats?: number;
}

export interface PlanInput {
  endpointUrl: string;
  model: string;
  suiteId: string;
  objective: ProfileObjective;
  budget: PlanBudgetInput;
  options?: Record<string, unknown>;
}

/** `POST /api/bench/plan` — §01/§09: pure bookkeeping server-side, never a
 *  launch. Returns a `BenchmarkRun` in `state: "planned"`; nothing calls
 *  `startBench` from here. */
export function planBench(input: PlanInput, signal?: AbortSignal): Promise<BenchmarkRun> {
  const budget: Record<string, number> = { repeats: input.budget.repeats ?? 1 };
  if (input.budget.maxCases) budget.max_cases = input.budget.maxCases;
  if (input.budget.maxSeconds) budget.max_seconds = input.budget.maxSeconds;
  if (input.budget.maxGeneratedTokens) budget.max_generated_tokens = input.budget.maxGeneratedTokens;
  return post<{ run: BenchmarkRun }>(
    '/api/bench/plan',
    {
      endpoint_url: input.endpointUrl,
      model: input.model,
      suite_id: input.suiteId,
      objective: input.objective,
      budget,
      options: input.options ?? {},
    },
    signal,
  ).then((d) => d.run);
}

/** `POST /api/bench/runs/{id}/start` — the ONE explicit, human-initiated
 *  call in this whole module that ever starts a benchmark. `Optimize.tsx`
 *  wires this only to the `bench-start` button's `onClick`; see this
 *  file's own module doc comment. */
export function startBench(runId: string): Promise<BenchmarkRun> {
  return post<{ run: BenchmarkRun }>(`/api/bench/runs/${encodeURIComponent(runId)}/start`).then((d) => d.run);
}

export function listRuns(limit = 50, signal?: AbortSignal): Promise<BenchmarkRun[]> {
  return get<{ runs: BenchmarkRun[] }>(`/api/bench/runs?limit=${encodeURIComponent(String(limit))}`, signal).then((d) => d.runs);
}

/** `GET /api/bench/runs/{id}` — the read-only status check. Mounting the
 *  screen, remounting it, or reconnecting after a reload all funnel
 *  through this and NEVER through `startBench` (T19). */
export function getRun(runId: string, signal?: AbortSignal): Promise<BenchmarkRun> {
  return get<{ run: BenchmarkRun }>(`/api/bench/runs/${encodeURIComponent(runId)}`, signal).then((d) => d.run);
}

export function cancelRun(runId: string): Promise<BenchmarkRun> {
  return post<{ run: BenchmarkRun }>(`/api/bench/runs/${encodeURIComponent(runId)}/cancel`).then((d) => d.run);
}

/** `GET /api/bench/compare?baseline=&candidate=` — pure, recomputed fresh
 *  every call (never a cached verdict; §13's "evidencia vigente"). */
export function compareRuns(baselineRunId: string, candidateRunId: string, signal?: AbortSignal): Promise<Comparison> {
  const p = new URLSearchParams({ baseline: baselineRunId, candidate: candidateRunId });
  return get<{ comparison: Comparison }>(`/api/bench/compare?${p.toString()}`, signal).then((d) => d.comparison);
}

/** `POST /api/bench/profiles/{id}/promote` — the server recomputes the
 *  comparison itself from the two run ids; a client-side `verdict` is
 *  never trusted or sent. `Optimize.tsx` only enables the button that
 *  calls this once its own last-fetched `Comparison.verdict ===
 *  "improvement"` (`canPromote` below), but the server's own re-check is
 *  what actually decides. */
export function promoteProfile(profileId: string, candidateRunId: string, baselineRunId: string): Promise<InferenceProfile> {
  return post<{ profile: InferenceProfile }>(`/api/bench/profiles/${encodeURIComponent(profileId)}/promote`, {
    candidate_run_id: candidateRunId,
    baseline_run_id: baselineRunId,
  }).then((d) => d.profile);
}

// ── pure presentation helpers — exercised by studio/checks/bench.check.mjs
// without a DOM. ───────────────────────────────────────────────────────────

/**
 * §09: `RunSummary.estimate_seconds` is `null` until this exact model's
 * decode speed has actually been learned by a run — `plan()` never
 * guesses. The contract's own wording for that state (CONTRATO_INF04
 * Lote B) is shown verbatim rather than a bare "—", so a person reads it
 * as "nothing is known yet", not as an error.
 */
export function estimateLabel(estimateSeconds: number | null): string {
  if (estimateSeconds === null || !Number.isFinite(estimateSeconds)) return t('unknown until a first run');
  if (estimateSeconds < 1) return t('under a second');
  if (estimateSeconds < 60) return t('~{n}s', { n: String(Math.round(estimateSeconds)) });
  const minutes = estimateSeconds / 60;
  if (minutes < 60) return t('~{n} min', { n: String(Math.round(minutes * 10) / 10) });
  const hours = minutes / 60;
  return t('~{n} h', { n: String(Math.round(hours * 10) / 10) });
}

/** "+12.3%" / "-4.5%" / "n/a" for a `null` delta — never a bare number a
 *  reader could mistake for an absolute value rather than a percentage
 *  change. */
export function formatDelta(pct: number | null): string {
  if (pct === null || !Number.isFinite(pct)) return t('n/a');
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(1)}%`;
}

/** A coarse tone for the comparator card — `ok` only for a genuine
 *  `improvement`, `danger` for a measured `regression`, `warning` for
 *  everything compare() is honest about not being sure of
 *  (`no_change`/`inconclusive`). Never a fourth "neutral" bucket here: the
 *  comparator always has an opinion once it has run, unlike a run's own
 *  in-flight `runStateTone`. */
export function verdictTone(verdict: ComparisonVerdict): 'ok' | 'warning' | 'danger' {
  if (verdict === 'improvement') return 'ok';
  if (verdict === 'regression') return 'danger';
  return 'warning';
}

/** §13's promotion rule, restated as the one boolean the "Mark as
 *  recommended" button checks: `verdict === "improvement"`, nothing else —
 *  not "faster", not "n is big enough", not "the server will probably
 *  agree". The server re-derives the same verdict itself before it ever
 *  promotes anything (`promoteProfile`'s own doc comment); this only
 *  decides whether the button is enabled. */
export function canPromote(comparison: Pick<Comparison, 'verdict' | 'comparable'> | null): boolean {
  return Boolean(comparison && comparison.comparable && comparison.verdict === 'improvement');
}
