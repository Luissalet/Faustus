import { ApiError, getJson, responseReason } from './api';
import { t } from '../i18n';

/**
 * Saved workflows, the tools they publish, their evaluation sets and the
 * templates the `/workflows` screen starts from — the Studio half of
 * `routes/workflows_routes.py` (`docs/api/workflows-model-nodes.md`).
 *
 * Shapes only: the server validates (a definition, a set, a template's
 * parameters) and says why it refused, and this file shows what it said. A
 * definition stays an opaque `Record<string, unknown>` for the same reason
 * `adapters/topology.ts` keeps it one.
 */

const BASE = '/api/workflows';

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

async function send<T>(method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    signal,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

// ── the library and the tools it publishes ────────────────────────────────

export interface SavedWorkflow {
  name: string;
  tool: string;
  title: string;
  description: string;
  workflowId: string;
  version: string;
  enabled: boolean;
  allowOverrides: boolean;
  /** Declares an `inputs` schema, which is what a tool needs. */
  publishable: boolean;
  nodes: number;
  updatedAt: string;
}

function savedFrom(raw: Record<string, unknown>): SavedWorkflow {
  return {
    name: str(raw.name), tool: str(raw.tool), title: str(raw.title), description: str(raw.description),
    workflowId: str(raw.workflow_id), version: str(raw.version), enabled: raw.enabled === true,
    allowOverrides: raw.allow_overrides !== false, publishable: raw.publishable === true,
    nodes: num(raw.nodes), updatedAt: str(raw.updated_at),
  };
}

export async function listSavedWorkflows(signal?: AbortSignal): Promise<SavedWorkflow[]> {
  const body = await getJson<{ workflows?: unknown }>(`${BASE}/library`, signal);
  return list(body.workflows).map((w) => savedFrom(obj(w)));
}

export async function getSavedWorkflow(name: string, signal?: AbortSignal): Promise<SavedWorkflow & { definition: Record<string, unknown> }> {
  const body = await getJson<{ workflow?: unknown }>(`${BASE}/library/${encodeURIComponent(name)}`, signal);
  const raw = obj(body.workflow);
  return { ...savedFrom(raw), definition: obj(raw.definition) };
}

export async function saveWorkflow(
  definition: Record<string, unknown>,
  options?: { name?: string; enabled?: boolean; allowOverrides?: boolean },
): Promise<SavedWorkflow> {
  const payload: Record<string, unknown> = { definition };
  if (options?.name) payload.name = options.name;
  if (options?.enabled !== undefined) payload.enabled = options.enabled;
  if (options?.allowOverrides !== undefined) payload.allow_overrides = options.allowOverrides;
  const body = await send<{ workflow?: unknown }>('POST', `${BASE}/library`, payload);
  return savedFrom(obj(body.workflow));
}

export async function updateSavedWorkflow(name: string, patch: { enabled?: boolean; allowOverrides?: boolean }): Promise<SavedWorkflow> {
  const payload: Record<string, unknown> = {};
  if (patch.enabled !== undefined) payload.enabled = patch.enabled;
  if (patch.allowOverrides !== undefined) payload.allow_overrides = patch.allowOverrides;
  const body = await send<{ workflow?: unknown }>('PATCH', `${BASE}/library/${encodeURIComponent(name)}`, payload);
  return savedFrom(obj(body.workflow));
}

export async function deleteSavedWorkflow(name: string): Promise<void> {
  await send('DELETE', `${BASE}/library/${encodeURIComponent(name)}`);
}

export interface PublishedTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export async function listPublishedTools(signal?: AbortSignal): Promise<PublishedTool[]> {
  const body = await getJson<{ tools?: unknown }>(`${BASE}/published`, signal);
  return list(body.tools).map((raw) => {
    const row = obj(raw);
    return { name: str(row.name), description: str(row.description), inputSchema: obj(row.inputSchema) };
  });
}

// ── templates ─────────────────────────────────────────────────────────────

export interface TemplateParameter {
  name: string;
  label: string;
  kind: 'folder' | 'text' | 'integer' | 'number';
  help: string;
  required: boolean;
  defaultValue: string | number | null;
  minimum: number | null;
  maximum: number | null;
}

export interface WorkflowTemplate {
  id: string;
  title: string;
  description: string;
  category: string;
  notes: string[];
  parameters: TemplateParameter[];
  nodes: { id: string; type: string }[];
}

export async function listTemplates(signal?: AbortSignal): Promise<WorkflowTemplate[]> {
  const body = await getJson<{ templates?: unknown }>(`${BASE}/templates`, signal);
  return list(body.templates).map((raw) => {
    const row = obj(raw);
    return {
      id: str(row.id), title: str(row.title), description: str(row.description), category: str(row.category),
      notes: list(row.notes).map(String),
      parameters: list(row.parameters).map((p) => {
        const param = obj(p);
        const kind = str(param.kind);
        const fallback = param.default;
        return {
          name: str(param.name), label: str(param.label),
          kind: (kind === 'folder' || kind === 'integer' || kind === 'number' ? kind : 'text') as TemplateParameter['kind'],
          help: str(param.help), required: param.required !== false,
          defaultValue: typeof fallback === 'string' || typeof fallback === 'number' ? fallback : null,
          minimum: typeof param.minimum === 'number' ? param.minimum : null,
          maximum: typeof param.maximum === 'number' ? param.maximum : null,
        };
      }),
      nodes: list(row.nodes).map((n) => ({ id: str(obj(n).id), type: str(obj(n).type) })),
    };
  });
}

/** Fill a template in. The server refuses with every problem at once; the
 *  message it gives is what comes back in the thrown `ApiError`. */
export async function instantiateTemplate(id: string, parameters: Record<string, unknown>): Promise<Record<string, unknown>> {
  const body = await send<{ definition?: unknown }>('POST', `${BASE}/templates/${encodeURIComponent(id)}/instantiate`, { parameters });
  const definition = obj(body.definition);
  if (!Array.isArray(definition.nodes)) throw new ApiError(t('The server did not return a definition.'), 502);
  return definition;
}

// ── evaluation ────────────────────────────────────────────────────────────

export type ScorerType = 'exact' | 'contains' | 'regex' | 'json_schema' | 'numeric' | 'judge';

export interface Scorer {
  type: ScorerType;
  path?: string;
  expected?: unknown;
  pattern?: string;
  flags?: string;
  schema?: Record<string, unknown>;
  tolerance?: number;
  relative?: boolean;
  criteria?: string;
  threshold?: number;
  case_sensitive?: boolean;
}

export interface EvalCase {
  id: string;
  name?: string;
  inputs: Record<string, unknown>;
  mocks?: Record<string, Record<string, unknown>>;
  scorers?: Scorer[];
}

export interface EvalSetBody {
  scorers: Scorer[];
  cases: EvalCase[];
}

export interface EvalSetSummary {
  name: string;
  cases: number;
  scorers: number;
}

export async function listEvalSets(workflow: string, signal?: AbortSignal): Promise<{ sets: EvalSetSummary[]; modelJudge: boolean }> {
  const body = await getJson<{ sets?: unknown; model_judge?: unknown }>(`${BASE}/library/${encodeURIComponent(workflow)}/eval-sets`, signal);
  return {
    sets: list(body.sets).map((s) => ({ name: str(obj(s).name), cases: num(obj(s).cases), scorers: num(obj(s).scorers) })),
    modelJudge: body.model_judge === true,
  };
}

export async function getEvalSet(workflow: string, name: string, signal?: AbortSignal): Promise<EvalSetBody> {
  const body = await getJson<{ set?: unknown }>(`${BASE}/library/${encodeURIComponent(workflow)}/eval-sets/${encodeURIComponent(name)}`, signal);
  const inner = obj(obj(body.set).set);
  return { scorers: list(inner.scorers) as Scorer[], cases: list(inner.cases) as EvalCase[] };
}

export async function putEvalSet(workflow: string, name: string, set: EvalSetBody): Promise<void> {
  await send('PUT', `${BASE}/library/${encodeURIComponent(workflow)}/eval-sets/${encodeURIComponent(name)}`, set);
}

export async function deleteEvalSet(workflow: string, name: string): Promise<void> {
  await send('DELETE', `${BASE}/library/${encodeURIComponent(workflow)}/eval-sets/${encodeURIComponent(name)}`);
}

export interface EvalScore {
  id: string;
  type: string;
  path: string;
  passed: boolean;
  detail: string;
  score?: number;
}

export interface EvalCaseResult {
  id: string;
  name: string;
  status: 'passed' | 'failed' | 'error';
  runStatus: string;
  runId: string;
  scores: EvalScore[];
  output: unknown;
  error: string;
  note: string;
  durationMs: number;
}

export interface EvalSummary {
  mode: string;
  total: number;
  passed: number;
  failed: number;
  error: number;
  passRate: number;
  durationMsTotal: number;
  durationMsAvg: number;
  durationMsP95: number;
  unscored: number;
  note: string;
  judgeEnabled: boolean;
  byScorer: Record<string, { passed: number; total: number }>;
}

export interface EvalReport {
  id: string;
  set: string;
  status: string;
  startedAt: string;
  endedAt: string;
  summary: EvalSummary;
  cases: EvalCaseResult[];
}

function reportFrom(raw: Record<string, unknown>): EvalReport {
  const s = obj(raw.summary);
  const byScorer: Record<string, { passed: number; total: number }> = {};
  for (const [k, v] of Object.entries(obj(s.by_scorer))) byScorer[k] = { passed: num(obj(v).passed), total: num(obj(v).total) };
  return {
    id: str(raw.id), set: str(raw.set), status: str(raw.status), startedAt: str(raw.started_at), endedAt: str(raw.ended_at),
    summary: {
      mode: str(s.mode), total: num(s.total), passed: num(s.passed), failed: num(s.failed), error: num(s.error),
      passRate: num(s.pass_rate), durationMsTotal: num(s.duration_ms_total), durationMsAvg: num(s.duration_ms_avg),
      durationMsP95: num(s.duration_ms_p95), unscored: num(s.unscored), note: str(s.note),
      judgeEnabled: s.judge_enabled === true, byScorer,
    },
    cases: list(raw.cases).map((c) => {
      const row = obj(c);
      const status = str(row.status);
      return {
        id: str(row.id), name: str(row.name), status: (status === 'passed' || status === 'failed' ? status : 'error') as EvalCaseResult['status'],
        runStatus: str(row.run_status), runId: str(row.run_id), output: row.output, error: str(row.error), note: str(row.note),
        durationMs: num(row.duration_ms),
        scores: list(row.scores).map((x) => {
          const sc = obj(x);
          return { id: str(sc.id), type: str(sc.type), path: str(sc.path), passed: sc.passed === true, detail: str(sc.detail), score: typeof sc.score === 'number' ? sc.score : undefined };
        }),
      };
    }),
  };
}

export interface EvaluateOptions {
  set: string;
  mode: 'simulate' | 'real';
  /** Only sent with `mode: 'real'`, and only after the person ticked the box. */
  allowReal?: boolean;
  cases?: string[];
  timeoutS?: number;
  waitSeconds?: number;
}

export async function runEvaluation(workflow: string, options: EvaluateOptions): Promise<{ finished: boolean; report: EvalReport }> {
  const payload: Record<string, unknown> = { set: options.set, mode: options.mode, wait_seconds: options.waitSeconds ?? 20 };
  if (options.mode === 'real' && options.allowReal) payload.allow_real = true;
  if (options.cases?.length) payload.cases = options.cases;
  if (options.timeoutS) payload.timeout_s = options.timeoutS;
  const body = await send<{ finished?: unknown; report?: unknown }>('POST', `${BASE}/library/${encodeURIComponent(workflow)}/evaluate`, payload);
  return { finished: body.finished === true, report: reportFrom(obj(body.report)) };
}

export async function listEvaluations(workflow: string, signal?: AbortSignal): Promise<EvalReport[]> {
  const body = await getJson<{ reports?: unknown }>(`${BASE}/library/${encodeURIComponent(workflow)}/evaluations`, signal);
  return list(body.reports).map((r) => reportFrom(obj(r)));
}

export async function getEvaluation(id: string, signal?: AbortSignal): Promise<EvalReport> {
  const body = await getJson<{ report?: unknown }>(`${BASE}/evaluations/${encodeURIComponent(id)}`, signal);
  return reportFrom(obj(body.report));
}

// ── one run, with what each loop pass did ─────────────────────────────────

export interface LoopIteration {
  iteration: number;
  status: string;
  reason: string;
  untilPassed: boolean | null;
  nodes: { id: string; status: string; attempt: number; reason: string }[];
  seconds: number;
}

export interface RunDetail {
  status: string;
  reason: string;
  nodes: Record<string, { status: string; label: string; reason: string }>;
  loops: Record<string, LoopIteration[]>;
}

/** GET /api/workflows/runs/{id}: each node's state and the label a classify or
 *  guard chose (so an edge can say which way the run went), plus, for every
 *  loop, one row per pass with its body nodes. */
export async function getRunDetail(runId: string, signal?: AbortSignal): Promise<RunDetail> {
  return runDetailFrom(await getJson<Record<string, unknown>>(`${BASE}/runs/${encodeURIComponent(runId)}`, signal));
}

/** The same reading, for a caller that already fetched the run and wants the
 *  rest of the body (its definition) too. */
export function runDetailFrom(body: Record<string, unknown>): RunDetail {
  const nodes: RunDetail['nodes'] = {};
  for (const [id, raw] of Object.entries(obj(body.nodes))) {
    const row = obj(raw);
    nodes[id] = { status: str(row.status), label: str(obj(row.result).branch), reason: str(row.reason) };
  }
  const loops: RunDetail['loops'] = {};
  for (const [id, rows] of Object.entries(obj(body.loops))) {
    loops[id] = list(rows).map((r) => {
      const row = obj(r);
      const until = obj(row.until).passed;
      return {
        iteration: num(row.iteration), status: str(row.status), reason: str(row.reason),
        untilPassed: typeof until === 'boolean' ? until : null,
        nodes: Object.entries(obj(row.nodes)).map(([nid, s]) => ({ id: nid, status: str(obj(s).status), attempt: num(obj(s).attempt), reason: str(obj(s).reason) })),
        seconds: num(obj(row.budget_used).seconds),
      };
    });
  }
  return { status: str(obj(body.run).status), reason: str(obj(body.run).reason), nodes, loops };
}
